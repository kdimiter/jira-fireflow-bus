"""Bounded HTTPS-only JSON and form/text transport for Jira Cloud and FireFlow.

The configured URL is an origin, never an arbitrary request URL.  Callers provide a
validated API path and this module refuses redirects and inherited proxy settings so
credentials cannot move to a second destination.
"""
import hashlib
import hmac
import http.client
import json
import re
import ssl
import urllib.request
from urllib.parse import urlencode, urlsplit

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
METHODS = frozenset(('GET', 'POST', 'PUT', 'DELETE', 'PATCH'))
QUERY_SECRET = re.compile(
    r'password|passwd|secret|token|session|cookie|authoriz|api.?key|private.?key', re.I)
HEADER_NAME = re.compile(r'[A-Za-z0-9-]{1,64}')
HOP_BY_HOP = frozenset(('connection', 'content-length', 'host', 'proxy-authorization',
                        'te', 'trailer', 'transfer-encoding', 'upgrade'))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the redirect response as an error; never follow it with credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_certificate_pin(certificate, expected):
    """Verify a DER certificate after normal PKI and hostname verification succeeded."""
    if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('Expected administrator-provisioned SHA256 certificate pin')
    actual = hashlib.sha256(certificate or b'').hexdigest()
    if not certificate or not hmac.compare_digest(actual, expected):
        raise ssl.SSLCertVerificationError('Server certificate does not match configured pin')


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """Add a certificate pin to an already verifying TLS context."""

    def __init__(self, context, pin):
        self.context = context
        self.pin = pin
        super().__init__(context=context)

    def https_open(self, request):
        context, pin = self.context, self.pin

        class Connection(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                try:
                    verify_certificate_pin(self.sock.getpeercert(binary_form=True), pin)
                except BaseException:
                    self.close()
                    raise

        # Python 3.11+ HTTPSConnection takes the verification policy from ``context``;
        # passing the removed ``check_hostname`` constructor keyword breaks pin mode.
        return self.do_open(Connection, request, context=context)


def https_origin(value):
    """Validate and return one canonical representation of an HTTPS origin."""
    if not isinstance(value, str) or not value or any(ord(char) < 33 for char in value):
        raise ValueError('Expected configured HTTPS origin')
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ValueError('Expected configured HTTPS origin') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('Expected configured HTTPS origin')
    hostname = parsed.hostname
    if ':' in hostname:
        hostname = '[' + hostname.lower() + ']'
    else:
        try:
            hostname = hostname.encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise ValueError('Expected configured HTTPS origin') from None
    port = parsed.port
    return 'https://' + hostname + ((':' + str(port))
                                     if port is not None and port != 443 else '')


def _api_path(path):
    if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//')
            or not re.fullmatch(r'/[A-Za-z0-9._~/-]*', path)
            or any(part in ('', '.', '..') for part in path.split('/')[1:])):
        raise ValueError('Invalid API path')
    return path


def _query(query):
    if query is None:
        return ''
    if not isinstance(query, dict):
        raise ValueError('Expected query parameter mapping')
    pairs = []
    for name, raw in query.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.\[\]-]{1,64}', name):
            raise ValueError('Invalid query parameter name')
        if QUERY_SECRET.search(name):
            raise ValueError('Secrets must never travel in a query string')
        values = raw if isinstance(raw, (list, tuple)) else (raw,)
        if len(values) > 1000:
            raise ValueError('Too many query parameter values')
        for value in values:
            if isinstance(value, bool):
                value = 'true' if value else 'false'
            elif type(value) is int:
                value = str(value)
            if (not isinstance(value, str) or len(value) > 4096
                    or any(ord(char) < 32 for char in value)):
                raise ValueError('Invalid query parameter value')
            pairs.append((name, value))
    encoded = urlencode(pairs)
    if len(encoded) > 16 * 1024:
        raise ValueError('Query string limit exceeded')
    return encoded


def _headers(headers):
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise ValueError('Expected HTTP header mapping')
    result = {}
    for name, value in headers.items():
        if (not isinstance(name, str) or not HEADER_NAME.fullmatch(name)
                or name.casefold() in HOP_BY_HOP):
            raise ValueError('Invalid HTTP header')
        if (not isinstance(value, str) or len(value) > 8192
                or '\r' in value or '\n' in value or '\x00' in value):
            raise ValueError('Invalid HTTP header value')
        result[name] = value
    return result


def _complexity(value):
    nodes = 0
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError('JSON response is too complex')
        if isinstance(item, dict):
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _decode(response):
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError('HTTP response limit exceeded')
    if not data:
        return {}
    content_type = str(response.headers.get('Content-Type') or '')
    media_type = content_type.split(';', 1)[0].strip().casefold()
    if media_type != 'application/json' and not media_type.endswith('+json'):
        raise ValueError('Expected JSON response content type')
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError('Invalid JSON response') from None
    _complexity(value)
    return value


def _tls_context(config):
    pin_only = config.get('tls_pin_only', False)
    if type(pin_only) is not bool:
        raise ValueError('tls_pin_only must be true or false')
    pin = config.get('tls_certificate_sha256')
    if pin_only:
        if config.get('ca_file'):
            raise ValueError('tls_pin_only cannot be combined with ca_file')
        if (not isinstance(pin, str) or not re.fullmatch(r'[a-f0-9]{64}', pin)
                or pin == '0' * 64):
            raise ValueError('Trust server certificate requires an exact SHA256 certificate pin')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        context = ssl.create_default_context(cafile=config.get('ca_file'))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if hasattr(ssl, 'OP_NO_COMPRESSION'):
        context.options |= ssl.OP_NO_COMPRESSION
    return context


def _request(config, path, headers, method, payload, query, timeout, decoder):
    """One outbound call, with a common TLS, redirect and size boundary."""
    if not isinstance(config, dict):
        raise ValueError('Expected transport configuration')
    base = https_origin(config.get('base_url', ''))
    path = _api_path(path)
    if method not in METHODS:
        raise ValueError('Unsupported HTTP method')
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError('Invalid request timeout')
    fields = _headers(headers)
    if payload is not None and len(payload) > MAX_REQUEST_BYTES:
        raise ValueError('HTTP request limit exceeded')
    encoded_query = _query(query)
    url = base + path + (('?' + encoded_query) if encoded_query else '')
    context = _tls_context(config)
    pin = config.get('tls_certificate_sha256')
    if pin is None:
        https_handler = urllib.request.HTTPSHandler(context=context)
    else:
        if not isinstance(pin, str) or not re.fullmatch(r'[a-f0-9]{64}', pin):
            raise ValueError('Invalid certificate pin')
        https_handler = PinnedHTTPSHandler(context, pin)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                         https_handler)
    request = urllib.request.Request(url, headers=fields, method=method, data=payload)
    with opener.open(request, timeout=timeout) as response:
        return decoder(response)


def request_json(config, path, headers=None, method='GET', body=None, query=None,
                 timeout=10):
    """Make one bounded JSON request; mutations are deliberately never retried."""
    fields = _headers(headers)
    fields['Accept'] = 'application/json'
    payload = None
    if body is not None:
        try:
            payload = json.dumps(body, ensure_ascii=False, separators=(',', ':'),
                                 allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise ValueError('Invalid JSON request body') from None
        fields['Content-Type'] = 'application/json'
    return _request(config, path, fields, method, payload, query, timeout, _decode)


def _decode_json_string(response):
    """Decode a JSON string, tolerating Jira's unquoted scalar response.

    Jira's project-name validation endpoint is documented as returning an
    ``application/json`` string but some tenants return the same value without
    JSON quotes.  Keep this compatibility decoder limited to a bounded,
    printable scalar so general JSON callers remain strict.
    """
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError('HTTP response limit exceeded')
    content_type = str(response.headers.get('Content-Type') or '')
    media_type = content_type.split(';', 1)[0].strip().casefold()
    if media_type != 'application/json' and not media_type.endswith('+json'):
        raise ValueError('Expected JSON response content type')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Invalid UTF-8 JSON string response') from None
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        value = text
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise ValueError('Invalid JSON string response')
    return value


def request_json_string(config, path, headers=None, query=None, timeout=10):
    """Read one bounded printable string from a JSON scalar GET endpoint."""
    fields = _headers(headers)
    fields['Accept'] = 'application/json'
    return _request(config, path, fields, 'GET', None, query, timeout,
                    _decode_json_string)


def _decode_text(response):
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError('HTTP response limit exceeded')
    content_type = str(response.headers.get('Content-Type') or '')
    if content_type.split(';', 1)[0].strip().casefold() != 'text/plain':
        raise ValueError('Expected plain text response content type')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Invalid UTF-8 text response') from None


def request_text(config, path, headers=None, method='GET', body=None, query=None,
                 timeout=10):
    """Read UTF-8 plain text or POST a bounded URL-encoded form, without retries."""
    fields = _headers(headers)
    fields['Accept'] = 'text/plain'
    payload = None
    if body is not None:
        if (method != 'POST' or not isinstance(body, dict) or not 1 <= len(body) <= 20
                or any(not isinstance(key, str)
                       or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key)
                       or not isinstance(value, str) or '\x00' in value
                       or len(value) > MAX_REQUEST_BYTES
                       for key, value in body.items())):
            raise ValueError('Invalid form request body')
        try:
            payload = urlencode(body, encoding='utf-8', errors='strict').encode('ascii')
        except UnicodeEncodeError:
            raise ValueError('Invalid form request body') from None
        fields['Content-Type'] = 'application/x-www-form-urlencoded; charset=utf-8'
    return _request(config, path, fields, method, payload, query, timeout, _decode_text)


def get_json(config, path, headers=None, query=None, timeout=10):
    return request_json(config, path, headers=headers, query=query, timeout=timeout)
