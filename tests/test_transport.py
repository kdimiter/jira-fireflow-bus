"""Security contract for the bus-owned outbound JSON transport."""
import hashlib
import ssl
import unittest
from unittest.mock import patch, Mock

from algosec_jira_bus.transport import (MAX_RESPONSE_BYTES, NoRedirect,
                                        PinnedHTTPSHandler, https_origin, request_json,
                                        verify_certificate_pin)


class Response:
    def __init__(self, body=b'{"ok":true}', content_type='application/json'):
        self.body = body
        self.headers = {'Content-Type': content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.body[:limit]


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.response


class OriginAndRequest(unittest.TestCase):
    def call(self, response=None, **kwargs):
        opener = Opener(response or Response())
        with patch('algosec_jira_bus.transport.urllib.request.build_opener',
                   return_value=opener) as build:
            result = request_json({'base_url': 'https://asms.example.test'},
                                  '/FireFlow/api/templates', **kwargs)
        return result, opener, build.call_args.args

    def test_only_a_clean_https_origin_is_accepted(self):
        invalid = ('http://asms.example.test', 'https://user@asms.example.test',
                   'https://asms.example.test/path', 'https://asms.example.test?q=x',
                   'https://asms.example.test/#fragment', 'https:///missing-host')
        for base_url in invalid:
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                request_json({'base_url': base_url}, '/FireFlow/api/templates')

    def test_equivalent_https_origins_have_one_canonical_form(self):
        self.assertEqual(https_origin('https://ASMS.Example.TEST:443/'),
                         'https://asms.example.test')
        self.assertEqual(https_origin('https://ASMS.Example.TEST:8443'),
                         'https://asms.example.test:8443')
        self.assertEqual(https_origin('https://[2001:db8::1]:443/'),
                         'https://[2001:db8::1]')

    def test_path_cannot_change_origin_or_smuggle_a_query(self):
        for path in ('//other.example.test/x', 'https://other.example.test/x',
                     '/x?token=secret', '/x#fragment', '/x\\y', '/x/../admin',
                     '/x/%2f/admin', '/x\r\nInjected: yes'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                request_json({'base_url': 'https://asms.example.test'}, path)

    def test_proxy_environment_is_ignored_and_redirects_are_refused(self):
        _result, _opener, handlers = self.call()
        proxies = [handler for handler in handlers
                   if isinstance(handler, __import__('urllib.request').request.ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {})
        redirects = [handler for handler in handlers if isinstance(handler, NoRedirect)]
        self.assertEqual(len(redirects), 1)
        self.assertIsNone(redirects[0].redirect_request(None, None, 302, 'Found', {},
                                                        'https://other.example.test/'))

    def test_query_secret_names_are_rejected(self):
        for name in ('token', 'sessionId', 'api_key', 'password'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.call(query={name: 'x'})

    def test_request_body_is_bounded_before_network(self):
        with self.assertRaisesRegex(ValueError, 'request limit'):
            self.call(method='POST', body={'value': 'x' * 262144})

    def test_timeout_rejects_boolean_and_out_of_range_values(self):
        for timeout in (True, 0, 601, '30'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.call(timeout=timeout)

    def test_header_injection_and_authority_headers_are_rejected(self):
        for headers in ({'X-Test': 'ok\r\nInjected: yes'},
                        {'Host': 'other.example.test'}, {'Content-Length': '0'}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.call(headers=headers)


class ResponseBoundary(unittest.TestCase):
    def call(self, response):
        opener = Opener(response)
        with patch('algosec_jira_bus.transport.urllib.request.build_opener',
                   return_value=opener):
            return request_json({'base_url': 'https://asms.example.test'}, '/x')

    def test_response_is_bounded(self):
        with self.assertRaisesRegex(ValueError, 'response limit'):
            self.call(Response(b' ' * (MAX_RESPONSE_BYTES + 1)))

    def test_non_json_content_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'content type'):
            self.call(Response(b'{}', 'text/html; charset=utf-8'))

    def test_malformed_or_non_utf8_json_is_rejected(self):
        for body in (b'{', b'\xff'):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, 'JSON'):
                self.call(Response(body))

    def test_excessively_deep_json_is_rejected(self):
        body = (b'[' * 70) + b'0' + (b']' * 70)
        with self.assertRaisesRegex(ValueError, 'complex'):
            self.call(Response(body))

    def test_empty_success_body_is_allowed(self):
        self.assertEqual(self.call(Response(b'', '')),
                         {})


class TLS(unittest.TestCase):
    def test_certificate_pin_uses_constant_time_sha256_comparison(self):
        certificate = b'test certificate bytes'
        pin = hashlib.sha256(certificate).hexdigest()
        verify_certificate_pin(certificate, pin)
        with self.assertRaises(ssl.SSLCertVerificationError):
            verify_certificate_pin(certificate, '0' * 64)

    def test_pin_is_an_additional_check_on_normal_pki_and_hostname_validation(self):
        contexts = []

        def context(cafile=None):
            created = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            created.check_hostname = True
            created.verify_mode = ssl.CERT_REQUIRED
            contexts.append((cafile, created))
            return created

        opener = Opener(Response())
        config = {'base_url': 'https://asms.example.test', 'ca_file': '/private/ca.pem',
                  'tls_certificate_sha256': '0' * 64}
        with patch('algosec_jira_bus.transport.ssl.create_default_context', side_effect=context), \
             patch('algosec_jira_bus.transport.urllib.request.build_opener',
                   return_value=opener) as build:
            request_json(config, '/x')
        self.assertEqual(contexts[0][0], '/private/ca.pem')
        self.assertTrue(contexts[0][1].check_hostname)
        self.assertEqual(contexts[0][1].verify_mode, ssl.CERT_REQUIRED)
        handlers = build.call_args.args
        pinned = [handler for handler in handlers if isinstance(handler, PinnedHTTPSHandler)]
        self.assertEqual(len(pinned), 1)
        self.assertIs(pinned[0].context, contexts[0][1])

    def test_pinned_handler_uses_the_supported_https_connection_signature(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        handler = PinnedHTTPSHandler(context, '0' * 64)
        handler.do_open = Mock(return_value='response')

        self.assertEqual(handler.https_open(Mock()), 'response')
        _connection, _request = handler.do_open.call_args.args
        self.assertEqual(handler.do_open.call_args.kwargs, {'context': context})


if __name__ == '__main__':
    unittest.main()
