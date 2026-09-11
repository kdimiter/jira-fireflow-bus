"""Contract tests for the self-contained FireFlow REST client."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.fireflow import (FireFlow, TrafficRequest, digest,
                                       ticket_id)


def traffic_request(lines=1, template='Traffic Change Request', devices=('edge-fw-01',)):
    line = {'source': {'items': [{'address': '192.0.2.10'}]},
            'destination': {'items': [{'address': '198.51.100.20'}]},
            'service': {'items': [{'service': 'tcp/443'}]},
            'action': 'Allow'}
    return {'template': template,
            'fields': [{'name': 'subject', 'values': ['NET-12: test']},
                       {'name': 'devices', 'values': list(devices)}],
            'traffic': [dict(line) for _ in range(lines)]}


class Audit:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.records = []

    def record(self, *args):
        self.records.append(args)


class API:
    def __init__(self, fail_create=None):
        self.calls = []
        self.fail_create = fail_create

    def __call__(self, config, path, **kwargs):
        self.calls.append((config, path, kwargs))
        if path.endswith('/authentication/authenticate'):
            return {'status': 'Success', 'messages': [],
                    'data': {'sessionId': 'session_1'}}
        if path.endswith('/templates'):
            return {'status': 'Success', 'messages': [], 'data': [
                {'name': 'Traffic Change Request', 'enabled': True,
                 'type': 'Traffic Change'}]}
        if path.endswith('/change-requests/traffic') and kwargs.get('method') == 'POST':
            if self.fail_create:
                raise self.fail_create
            return {'status': 'Success', 'messages': [], 'data': {'id': 42}}
        if '/change-requests/traffic/' in path:
            return {'status': 'Success', 'messages': [],
                    'data': {'id': 42, 'fields': []}}
        raise AssertionError(path)


class Schema(unittest.TestCase):
    def test_valid_request_is_normalized_without_external_packages(self):
        value = TrafficRequest.model_validate(traffic_request()).model_dump()
        line = value['traffic'][0]
        self.assertEqual(line['application']['items'][0]['name'], 'any')
        self.assertEqual(line['user']['items'][0]['name'], 'any')

    def test_named_source_and_destination_objects_are_accepted(self):
        request = traffic_request()
        request['traffic'][0]['source']['items'] = [{'name': 'srv-app-01'}]
        request['traffic'][0]['destination']['items'] = [{'name': 'db-cluster'}]
        line = TrafficRequest.model_validate(request).model_dump()['traffic'][0]
        self.assertEqual(line['source']['items'][0]['name'], 'srv-app-01')
        self.assertEqual(line['destination']['items'][0]['name'], 'db-cluster')

    def test_source_item_requires_exactly_one_address_or_name(self):
        for item in ({}, {'address': '192.0.2.1', 'name': 'host'}, {'hostname': 'host'}):
            request = traffic_request()
            request['traffic'][0]['source']['items'] = [item]
            with self.subTest(item=item), self.assertRaises(ValueError):
                TrafficRequest.model_validate(request)

    def test_traffic_is_bounded_to_the_verified_100_line_contract(self):
        TrafficRequest.model_validate(traffic_request(100))
        with self.assertRaises(ValueError):
            TrafficRequest.model_validate(traffic_request(101))

    def test_large_discovered_device_lists_do_not_share_the_traffic_line_limit(self):
        TrafficRequest.model_validate(traffic_request(
            devices=tuple('fw-%03d' % number for number in range(101))))
        with self.assertRaises(ValueError):
            TrafficRequest.model_validate(traffic_request(
                devices=tuple('fw-%04d' % number for number in range(1001))))

    def test_extra_keys_and_workflow_fields_are_rejected(self):
        extra = traffic_request()
        extra['unexpected'] = True
        with self.assertRaises(ValueError):
            TrafficRequest.model_validate(extra)

    def test_ticket_id_refuses_bool_zero_and_overflow(self):
        for value in (True, 0, -1, 2147483648, '42'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ticket_id(value)

    def test_digest_is_canonical_json_sha256(self):
        expected = hashlib.sha256(b'["https://asms.example.test","jira-NET_12"]').hexdigest()
        self.assertEqual(digest(['https://asms.example.test', 'jira-NET_12']), expected)
        self.assertEqual(digest({'b': 2, 'a': 1}), digest({'a': 1, 'b': 2}))

    def test_digest_matches_the_pre_upgrade_production_vector(self):
        self.assertEqual(
            digest(['https://asms.example.test', 'jira-NET_12']),
            '024c78077a580f3504118595a15199b279be7127cd37a09906919782039b77b2')


class Client(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = API()
        self.config = {
            'base_url': 'https://asms.example.test',
            'username': 'jira-service',
            'password_ref': 'env:FIREFLOW_PASSWORD',
            'template': 'Traffic Change Request',
            'devices': ['edge-fw-01'],
            'allowed_templates': ['Traffic Change Request'],
            'allowed_devices': ['edge-fw-01'],
            'allowed_fields': ['subject', 'devices'],
        }
        self.client = FireFlow(self.config, 'MANAGE', Audit(self.temp.name),
                               request=self.api, resolver=lambda _ref: 'test-password')

    def tearDown(self):
        self.temp.cleanup()

    def test_authenticates_then_sends_only_the_fireflow_session_cookie(self):
        reply = self.client.templates()
        self.assertEqual(reply['status'], 'Success')
        auth, templates = self.api.calls
        self.assertEqual(auth[1], '/FireFlow/api/authentication/authenticate')
        self.assertEqual(auth[2]['body'], {'username': 'jira-service',
                                           'password': 'test-password'})
        self.assertEqual(templates[2]['headers'], {'Cookie': 'FireFlow_Session=session_1'})

    def test_pin_only_tls_reaches_authentication_and_template_requests(self):
        config = dict(self.config)
        config.update(tls_pin_only=True, tls_certificate_sha256='a' * 64)
        client = FireFlow(config, 'MANAGE', Audit(self.temp.name),
                          request=self.api, resolver=lambda _ref: 'test-password')

        client.templates()

        for transport, _path, _kwargs in self.api.calls:
            self.assertIs(transport['tls_pin_only'], True)
            self.assertEqual(transport['tls_certificate_sha256'], 'a' * 64)

    def test_get_accepts_only_a_positive_integer_and_returns_a_stable_digest(self):
        reply = self.client.get(42)
        self.assertEqual(reply['change_request_id'], 42)
        self.assertRegex(reply['sha256'], r'^[a-f0-9]{64}$')
        self.assertEqual(self.api.calls[-1][1], '/FireFlow/api/change-requests/traffic/42')

    def test_requestor_email_is_allowed_and_sent_to_fireflow(self):
        config = dict(self.config)
        config['allowed_fields'] = self.config['allowed_fields'] + ['Requestor']
        client = FireFlow(config, 'MANAGE', Audit(self.temp.name),
                          request=self.api, resolver=lambda _ref: 'test-password')
        request = traffic_request()
        request['fields'].append({
            'name': 'Requestor', 'values': ['creator@example.org']})

        client.create(request, 'jira-id-1234', 'Approved Jira request NET-1')

        create = next(call for call in self.api.calls
                      if call[1] == '/FireFlow/api/change-requests/traffic')
        self.assertIn({'name': 'Requestor', 'values': ['creator@example.org']},
                      create[2]['body']['fields'])

    def test_each_endpoint_rejects_an_incomplete_or_wrongly_typed_envelope(self):
        class Broken(API):
            def __init__(self, response):
                super().__init__()
                self.response = response

            def __call__(self, config, path, **kwargs):
                if path.endswith('/authentication/authenticate'):
                    return {'status': 'Success', 'messages': [],
                            'data': {'sessionId': 'session_1'}}
                return self.response

        cases = [
            ('templates missing messages', '/templates', {'status': 'Success', 'data': []}),
            ('templates data type', '/templates',
             {'status': 'Success', 'messages': [], 'data': {}}),
            ('get missing data', '/change-requests/traffic/42',
             {'status': 'Success', 'messages': []}),
            ('get data type', '/change-requests/traffic/42',
             {'status': 'Success', 'messages': [], 'data': []}),
        ]
        for name, path, response in cases:
            client = FireFlow(self.config, 'MANAGE', Audit(self.temp.name),
                              request=Broken(response), resolver=lambda _ref: 'test-password')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError,
                                                                  'INVALID_RESPONSE'):
                client._wire(path)

    def test_authentication_requires_the_documented_envelope(self):
        class BrokenAuth(API):
            def __call__(self, config, path, **kwargs):
                return {'status': 'Success', 'data': {'sessionId': 'session_1'}}

        client = FireFlow(self.config, 'MANAGE', Audit(self.temp.name),
                          request=BrokenAuth(), resolver=lambda _ref: 'test-password')
        with self.assertRaisesRegex(ValueError, 'INVALID_RESPONSE'):
            client.templates()

    def test_get_rejects_a_ticket_whose_id_does_not_match_the_path(self):
        class WrongTicket(API):
            def __call__(self, config, path, **kwargs):
                if '/change-requests/traffic/' in path:
                    return {'status': 'Success', 'messages': [],
                            'data': {'id': 999, 'fields': []}}
                return super().__call__(config, path, **kwargs)

        client = FireFlow(self.config, 'MANAGE', Audit(self.temp.name),
                          request=WrongTicket(), resolver=lambda _ref: 'test-password')
        with self.assertRaisesRegex(ValueError, 'REQUEST_ID_MISMATCH'):
            client.get(42)

    def test_authenticated_request_exception_discards_the_session(self):
        class Disconnect(API):
            def __call__(self, config, path, **kwargs):
                if path.endswith('/templates'):
                    raise TimeoutError('lost response')
                return super().__call__(config, path, **kwargs)

        client = FireFlow(self.config, 'MANAGE', Audit(self.temp.name),
                          request=Disconnect(), resolver=lambda _ref: 'test-password')
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            client.templates()
        self.assertIsNone(client.session)

    def test_create_enforces_template_device_and_field_allowlists_before_receipt(self):
        cases = []
        wrong_template = traffic_request(template='Other')
        cases.append(wrong_template)
        wrong_device = traffic_request(devices=('unknown-fw',))
        cases.append(wrong_device)
        wrong_field = traffic_request()
        wrong_field['fields'].append({'name': 'Owner', 'values': ['admin']})
        cases.append(wrong_field)
        workflow = traffic_request()
        workflow['fields'].append({'name': 'status', 'values': ['resolved']})
        cases.append(workflow)
        for request in cases:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.client.create(request, 'jira-NET_12', 'Requested in Jira NET-12')
        self.assertEqual(list(self.client.state.glob('*.json')), [])

    def test_wildcard_allowlists_are_refused(self):
        for key in ('allowed_templates', 'allowed_devices', 'allowed_fields'):
            config = dict(self.config)
            config[key] = ['*']
            client = FireFlow(config, 'MANAGE', Audit(self.temp.name), request=self.api,
                              resolver=lambda _ref: 'test-password')
            with self.subTest(key=key), self.assertRaises(ValueError):
                client.create(traffic_request(), 'jira-' + key,
                              'Requested in Jira NET-12')

    def test_create_writes_durable_started_and_result_receipts(self):
        result = self.client.create(traffic_request(), 'jira-NET_12',
                                    'Requested in Jira NET-12')
        key = digest(['https://asms.example.test', 'jira-NET_12'])
        started = self.client.state / (key + '.started.json')
        finished = self.client.state / (key + '.result.json')
        self.assertTrue(started.is_file())
        self.assertTrue(finished.is_file())
        self.assertEqual(started.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(started.read_text())['request_sha256'],
                         digest(TrafficRequest.model_validate(traffic_request()).model_dump()))
        self.assertEqual(result['operation_id'], 'jira-NET_12')
        self.assertEqual(result['receipt'], key)

    def test_duplicate_operation_id_is_refused_without_a_second_post(self):
        self.client.create(traffic_request(), 'jira-NET_12', 'Requested in Jira NET-12')
        with self.assertRaisesRegex(ValueError, 'already used'):
            self.client.create(traffic_request(), 'jira-NET_12', 'Requested in Jira NET-12')
        posts = [call for call in self.api.calls
                 if call[1].endswith('/change-requests/traffic')
                 and call[2].get('method') == 'POST']
        self.assertEqual(len(posts), 1)

    def test_raw_origin_receipt_prevents_a_duplicate_post_after_upgrade(self):
        config = dict(self.config, base_url='https://ASMS.Example.Test:443')
        client = FireFlow(config, 'MANAGE', Audit(self.temp.name), request=self.api,
                          resolver=lambda _ref: 'test-password')
        key = digest(['https://ASMS.Example.Test:443', 'jira-NET_legacy'])
        (client.state / (key + '.started.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'previous release'):
            client.create(traffic_request(), 'jira-NET_legacy',
                          'Requested in Jira NET-legacy')
        posts = [call for call in self.api.calls
                 if call[1].endswith('/change-requests/traffic')
                 and call[2].get('method') == 'POST']
        self.assertEqual(posts, [])

    def test_failed_post_leaves_started_receipt_and_reports_unknown_outcome(self):
        self.api.fail_create = TimeoutError('lost response')
        with self.assertRaisesRegex(ValueError, 'outcome may be unknown'):
            self.client.create(traffic_request(), 'jira-NET_13', 'Requested in Jira NET-13')
        key = digest(['https://asms.example.test', 'jira-NET_13'])
        self.assertTrue((self.client.state / (key + '.started.json')).exists())
        self.assertFalse((self.client.state / (key + '.result.json')).exists())

    def test_real_audit_records_digests_without_traffic_topology(self):
        from algosec_jira_bus.runtime import Audit as RealAudit

        client = FireFlow(self.config, 'MANAGE', RealAudit(self.temp.name),
                          request=self.api, resolver=lambda _ref: 'test-password')
        client.create(traffic_request(), 'jira-NET_14', 'Requested in Jira NET-14')
        audit = (Path(self.temp.name) / 'audit.jsonl').read_text()
        self.assertIn(digest(TrafficRequest.model_validate(traffic_request()).model_dump()),
                      audit)
        self.assertNotIn('192.0.2.10', audit)
        self.assertNotIn('198.51.100.20', audit)

    def test_real_audit_records_unknown_outcome_without_traffic_topology(self):
        from algosec_jira_bus.runtime import Audit as RealAudit

        self.api.fail_create = TimeoutError('lost response')
        client = FireFlow(self.config, 'MANAGE', RealAudit(self.temp.name),
                          request=self.api, resolver=lambda _ref: 'test-password')
        with self.assertRaisesRegex(ValueError, 'outcome may be unknown'):
            client.create(traffic_request(), 'jira-NET_15', 'Requested in Jira NET-15')
        audit = (Path(self.temp.name) / 'audit.jsonl').read_text()
        self.assertIn('outcome_unknown', audit)
        self.assertNotIn('192.0.2.10', audit)
        self.assertNotIn('198.51.100.20', audit)


if __name__ == '__main__':
    unittest.main()
