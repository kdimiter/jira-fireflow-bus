"""Minimal FireFlow REST client owned by the Jira bus.

The modern API owns creation and reads. Optional legacy RT operations mirror internal
comments and explicitly mapped statuses. Each mutation is durably claimed before its
POST, so an interrupted call cannot be retried as though nothing happened.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .runtime import secure_dir
from .transport import MAX_REQUEST_BYTES, https_origin, request_json, request_text

MAX_TEXT = 4096
MAX_LIST = 100
CHANGE_REQUEST_MAX = 2147483647
OPERATION_ID = re.compile(r'[A-Za-z0-9_-]{1,80}')
RT_STATUS = re.compile(r'[a-z][a-z -]{0,79}')
SESSION_ID = re.compile(r'[A-Za-z0-9_-]{1,256}')
ERROR_CODE = re.compile(r'[A-Za-z0-9_.-]{1,80}')
SECRET_NAME = re.compile(r'password|passwd|secret|token|cookie|session|private.?key', re.I)


class FireFlowMutationUnknown(ValueError):
    """A mutation may have applied; park it for reconciliation without another POST."""


def _rt_records(reply):
    """Parse RT fields, retaining continuation text without treating it as headers."""
    records = []
    current = None
    key = None
    for line in reply.splitlines():
        if line.startswith('id: '):
            current = {'id': line[4:]}
            records.append(current)
            key = 'id'
        elif current is not None and line.startswith((' ', '\t')) and key is not None:
            current[key] += '\n' + line[1:]
        elif current is not None:
            match = re.fullmatch(r'([A-Za-z][A-Za-z0-9_. -]*): ?(.*)', line)
            if match:
                key, value = match.groups()
                if key in current:
                    raise ValueError('FireFlow rejected operation: RT_DUPLICATE_FIELD')
                current[key] = value
            else:
                key = None
    return records


def digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=True, allow_nan=False).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def receipt_exists(path):
    """Fail closed when a receipt path exists or cannot be inspected safely."""
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def ticket_id(value):
    if type(value) is not int or not 1 <= value <= CHANGE_REQUEST_MAX:
        raise ValueError('Expected positive integer change request ID')
    return value


def _text(value, label='text'):
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_TEXT
            or '\x00' in value):
        raise ValueError('Invalid ' + label)
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Invalid ' + label) from None
    return value


def _mapping(value, required, optional=(), label='object'):
    if not isinstance(value, dict):
        raise ValueError('Invalid ' + label)
    allowed = set(required) | set(optional)
    if set(value) - allowed or any(name not in value for name in required):
        raise ValueError('Invalid ' + label)
    return value


def _list(value, label, minimum=0, maximum=MAX_LIST):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError('Invalid ' + label)
    return value


def _custom_fields(value):
    fields = []
    for raw in _list(value, 'fields'):
        raw = _mapping(raw, ('name', 'values'), label='custom field')
        fields.append({'name': _text(raw['name'], 'field name'),
                       'values': [_text(item, 'field value')
                                  for item in _list(raw['values'], 'field values',
                                                    maximum=1000)]})
    return fields


def _items(group, kind, label):
    group = _mapping(group, ('items',), label=label)
    result = []
    for raw in _list(group['items'], label + ' items', minimum=1):
        kinds = (kind,) if isinstance(kind, str) else tuple(kind)
        if (not isinstance(raw, dict) or set(raw) - (set(kinds) | {'fields'})
                or sum(name in raw for name in kinds) != 1):
            raise ValueError('Invalid ' + label + ' item')
        selected = next(name for name in kinds if name in raw)
        item = {selected: _text(raw[selected], label + ' value'),
                'fields': _custom_fields(raw.get('fields', []))}
        result.append(item)
    return {'items': result}


def _traffic_line(raw):
    raw = _mapping(raw, ('source', 'destination', 'service', 'action'),
                   ('application', 'user', 'fields'), label='traffic line')
    action = raw['action']
    if action not in ('Allow', 'Drop'):
        raise ValueError('Invalid traffic action')
    return {
        'source': _items(raw['source'], ('address', 'name'), 'source'),
        'destination': _items(raw['destination'], ('address', 'name'), 'destination'),
        'service': _items(raw['service'], 'service', 'service'),
        'application': _items(raw.get('application', {'items': [{'name': 'any'}]}),
                              'name', 'application'),
        'user': _items(raw.get('user', {'items': [{'name': 'any'}]}), 'name', 'user'),
        'action': action,
        'fields': _custom_fields(raw.get('fields', [])),
    }


class TrafficRequest:
    """Small dependency-free equivalent of the previously external request schema."""

    def __init__(self, payload):
        self._payload = payload
        self.template = payload['template']
        self.fields = payload['fields']
        self.traffic = payload['traffic']

    @classmethod
    def model_validate(cls, value):
        value = _mapping(value, ('template', 'traffic'), ('fields',),
                         label='traffic request')
        payload = {
            'template': _text(value['template'], 'template'),
            'fields': _custom_fields(value.get('fields', [])),
            'traffic': [_traffic_line(line)
                        for line in _list(value['traffic'], 'traffic', minimum=1)],
        }
        return cls(payload)

    def model_dump(self, exclude_none=True):
        # Round-trip through JSON so a caller cannot mutate the validated object.
        return json.loads(json.dumps(self._payload, ensure_ascii=False))


def safe_response(value):
    """Keep useful FireFlow data while masking fields that are named like secrets."""
    if isinstance(value, dict):
        result = {}
        named_secret = SECRET_NAME.search(str(value.get('name', value.get('key', ''))))
        for key, child in value.items():
            if SECRET_NAME.search(str(key)):
                result[key] = '[REDACTED]'
            elif named_secret and key == 'values':
                result[key] = ['[REDACTED]']
            else:
                result[key] = safe_response(child)
        return result
    if isinstance(value, list):
        return [safe_response(item) for item in value]
    return value


class FireFlow:
    def __init__(self, config, mode, audit, request=None, resolver=None, text_request=None):
        if not isinstance(config, dict):
            raise ValueError('Expected FireFlow configuration')
        self.config = dict(config)
        # Local queue/release commands construct the adapter without touching the network.
        # Validate the origin at the first API boundary so those recovery commands remain
        # usable even while an operator is repairing an incomplete configuration.
        base_url = config.get('base_url', '')
        self.config['base_url'] = https_origin(base_url) if base_url else ''
        self.receipt_origins = tuple(dict.fromkeys(
            origin for origin in (self.config['base_url'], base_url) if origin))
        if mode not in ('READONLY', 'MANAGE', 'ROOT'):
            raise ValueError('Invalid FireFlow mode')
        self.mode = mode
        self.audit = audit
        self.request = request or request_json
        self.text_request = text_request or request_text
        self.resolver = resolver
        self.session = None
        root = getattr(audit, 'directory', None)
        if root is None:
            root = Path(audit.path).parent
        self.state = secure_dir(Path(root) / 'fireflow')

    def _secret(self, reference):
        if self.resolver is None:
            from .config import resolve_secret
            return resolve_secret(reference)
        return self.resolver(reference)

    def _transport(self):
        transport = {key: self.config[key]
                for key in ('base_url', 'ca_file', 'tls_certificate_sha256',
                            'tls_pin_only')
                if key in self.config}
        transport['base_url'] = https_origin(transport.get('base_url', ''))
        return transport

    def _check(self, reply, data_type):
        valid = (isinstance(reply, dict) and reply.get('status') == 'Success'
                 and isinstance(reply.get('messages'), list)
                 and 'data' in reply and isinstance(reply['data'], data_type))
        if not valid:
            self.session = None
            codes = []
            if isinstance(reply, dict):
                messages = reply.get('messages', [])
                for item in messages if isinstance(messages, list) else []:
                    code = item.get('code', '') if isinstance(item, dict) else ''
                    if isinstance(code, str) and ERROR_CODE.fullmatch(code):
                        codes.append(code)
            raise ValueError('FireFlow rejected operation: '
                             + ','.join(codes or ['INVALID_RESPONSE']))

    def _ensure_session(self):
        transport = self._transport()
        if self.session is None:
            if self.config.get('session_ref'):
                session = self._secret(self.config['session_ref'])
            else:
                if not self.config.get('username') or not self.config.get('password_ref'):
                    raise ValueError('FireFlow credentials are not configured')
                reply = self.request(
                    transport, '/FireFlow/api/authentication/authenticate', method='POST',
                    body={'username': self.config['username'],
                          'password': self._secret(self.config['password_ref'])})
                self._check(reply, dict)
                data = reply.get('data')
                session = data.get('sessionId') if isinstance(data, dict) else None
            if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
                raise ValueError('FireFlow authentication rejected')
            self.session = session

    def _wire(self, path, method='GET', body=None):
        if path not in ('/templates', '/change-requests/traffic') and not re.fullmatch(
                r'/change-requests/traffic/[1-9][0-9]{0,9}', path):
            raise ValueError('Unsupported FireFlow API path')
        transport = self._transport()
        self._ensure_session()
        try:
            reply = self.request(transport, '/FireFlow/api' + path, method=method, body=body,
                                 headers={'Cookie': 'FireFlow_Session=' + self.session})
        except Exception:
            # A transport exception can mean the server expired or invalidated the session.
            # Mutations are still never retried; the next independent operation reauthenticates.
            self.session = None
            raise
        self._check(reply, list if path == '/templates' else dict)
        return reply

    def _read(self, operation, path):
        request_id = uuid.uuid4().hex
        entry = {'op': operation}
        self.audit.record(request_id, 'received', entry)
        try:
            result = self._wire(path)
            self.audit.record(request_id, 'completed', entry)
            return safe_response(result)
        except Exception as error:
            self.audit.record(request_id, 'failed',
                              {**entry, 'error_type': type(error).__name__})
            if isinstance(error, ValueError) and str(error).startswith('FireFlow rejected'):
                raise
            raise ValueError('FireFlow unavailable; check credentials, TLS and audit') from None

    def templates(self):
        return self._read('fireflow_templates', '/templates')

    def get(self, change_request_id):
        identifier = ticket_id(change_request_id)
        request_id = uuid.uuid4().hex
        entry = {'op': 'fireflow_get'}
        self.audit.record(request_id, 'received', entry)
        try:
            raw = self._wire('/change-requests/traffic/' + str(identifier))
            if raw['data'].get('id') != identifier:
                raise ValueError('FireFlow rejected operation: REQUEST_ID_MISMATCH')
            self.audit.record(request_id, 'completed', entry)
            return {'response': safe_response(raw), 'sha256': digest(raw),
                    'change_request_id': identifier}
        except Exception as error:
            self.audit.record(request_id, 'failed',
                              {**entry, 'error_type': type(error).__name__})
            if isinstance(error, ValueError) and str(error).startswith('FireFlow rejected'):
                raise
            raise ValueError('FireFlow unavailable; check credentials, TLS and audit') from None

    def _legacy_enabled(self):
        if self.config.get('legacy_rt_enabled') is not True:
            raise ValueError('Legacy RT integration is not enabled')

    def _legacy_read_enabled(self):
        if (self.config.get('legacy_rt_read_enabled') is not True
                and self.config.get('legacy_rt_enabled') is not True):
            raise ValueError('Legacy RT read integration is not enabled')

    def _rt_wire(self, identifier, suffix='', method='GET', body=None, query=None):
        if method == 'GET':
            self._legacy_read_enabled()
        else:
            self._legacy_enabled()
        identifier = ticket_id(identifier)
        if suffix not in ('', '/history', '/edit', '/comment'):
            raise ValueError('Unsupported legacy RT path')
        self._ensure_session()
        try:
            reply = self.text_request(
                self._transport(), '/FireFlow/REST/1.0/ticket/' + str(identifier) + suffix,
                method=method, body=body, query=query,
                headers={'Cookie': 'RT_SID_FireFlow.443=' + self.session})
            if (not isinstance(reply, str)
                    or not re.match(r'\ART/[0-9.]+ 200 Ok(?:\r?\n|$)', reply)):
                raise ValueError('FireFlow rejected operation: RT_INVALID_RESPONSE')
            return reply
        except Exception:
            self.session = None
            raise

    def rt_get(self, change_request_id):
        """Read the raw legacy ticket, validating its identity before any mutation."""
        identifier = ticket_id(change_request_id)
        reply = self._rt_wire(identifier)
        records = _rt_records(reply)
        if len(records) != 1:
            raise ValueError('FireFlow rejected operation: RT_INVALID_TICKET')
        fields = records[0]
        if fields.get('id') != 'ticket/' + str(identifier):
            raise ValueError('FireFlow rejected operation: REQUEST_ID_MISMATCH')
        if not isinstance(fields.get('Status'), str) or not RT_STATUS.fullmatch(fields['Status']):
            raise ValueError('FireFlow rejected operation: RT_MISSING_STATUS')
        return fields

    def rt_history(self, change_request_id):
        """Read full history; bounded by the shared transport response limit."""
        return self._rt_wire(ticket_id(change_request_id), '/history', query={'format': 'l'})

    def rt_terminal_outcome(self, change_request_id):
        """Return the latest explicit MatchStatus recorded by FireFlow.

        The modern traffic-request response collapses ``already works`` into
        ``resolved`` and omits MatchStatus. RT history retains the authoritative
        workflow transaction, which lets the bus distinguish this outcome from an
        incompletely validated request.
        """
        records = _rt_records(self.rt_history(ticket_id(change_request_id)))
        matches = [(int(record['id']), record.get('NewValue')) for record in records
                   if re.fullmatch(r'[1-9][0-9]{0,19}', record.get('id', ''))
                   and record.get('Type') == 'Set'
                   and record.get('Field') == 'MatchStatus'
                   and isinstance(record.get('NewValue'), str)]
        if not matches:
            return None
        value = max(matches)[1]
        return value if RT_STATUS.fullmatch(value) else None

    def _rt_intent(self, identifier, operation_id, reason):
        self._authorize(reason)
        self._legacy_enabled()
        ticket_id(identifier)
        if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
            raise ValueError('Invalid operation ID')
        origin = https_origin(self.config.get('base_url', ''))
        key = digest([origin, operation_id])
        return key

    def _rt_receipt_read(self, path):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or info.st_size > MAX_REQUEST_BYTES):
                raise ValueError('Unsafe operation receipt')
            with os.fdopen(descriptor, 'r') as stream:
                descriptor = -1
                raw = stream.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError('Invalid operation receipt')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError('Invalid operation receipt')
            return result
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _rt_replay(self, operation_id, entry):
        for origin in self.receipt_origins:
            key = digest([origin, operation_id])
            started = self.state / (key + '.started.json')
            finished = self.state / (key + '.result.json')
            if receipt_exists(finished):
                result = self._rt_receipt_read(finished)
                if (any(result.get(name) != entry[name] for name in
                        ('op', 'change_request_id', 'request_sha256'))
                        or result.get('operation_id') != operation_id
                        or not isinstance(result.get('response'), dict)):
                    raise ValueError('Operation ID already used for another request')
                return {'response': result['response'], 'operation_id': operation_id,
                        'receipt': key}
            if receipt_exists(started):
                raise FireFlowMutationUnknown(
                    'Operation ID already used; outcome may be unknown. '
                    'Inspect ticket and receipt; do not retry automatically.')
        return None

    def _rt_mutate(self, identifier, operation_id, reason, operation, payload, suffix,
                   verify, before=None, noop=None):
        key = self._rt_intent(identifier, operation_id, reason)
        entry = {'op': operation, 'change_request_id': identifier,
                 'request_sha256': digest(payload),
                 'reason_sha256': hashlib.sha256(reason.encode('utf-8')).hexdigest()}
        previous = self._rt_replay(operation_id, entry)
        if previous is not None:
            return previous
        # Authenticate and confirm this exact ticket before claiming the mutation.
        ticket = self.rt_get(identifier)
        no_change = noop(ticket) if noop is not None else None
        if no_change is not None:
            result = {'change_request_id': identifier, **no_change}
            self.audit.record(operation_id, 'received', entry)
            self._receipt(self.state / (key + '.result.json'),
                          {**entry, 'operation_id': operation_id, 'response': result})
            self.audit.record(operation_id, 'completed', {**entry, 'receipt': key})
            return {'response': result, 'operation_id': operation_id, 'receipt': key}
        baseline = before() if before is not None else None
        self.audit.record(operation_id, 'received', entry)
        try:
            self._receipt(self.state / (key + '.started.json'),
                          {**entry, 'operation_id': operation_id})
        except FileExistsError:
            raise FireFlowMutationUnknown(
                'Operation ID already used; outcome may be unknown. Inspect receipt before any retry') from None
        try:
            self.audit.record(operation_id, 'authorized', entry)
            self._rt_wire(identifier, suffix, method='POST', body=payload)
            evidence = verify(baseline)
            result = {'change_request_id': identifier, **evidence}
            self._receipt(self.state / (key + '.result.json'),
                          {**entry, 'operation_id': operation_id, 'response': result})
            self.audit.record(operation_id, 'completed', {**entry, 'receipt': key})
            return {'response': result, 'operation_id': operation_id, 'receipt': key}
        except Exception as error:
            self.audit.record(operation_id, 'outcome_unknown',
                              {**entry, 'error_type': type(error).__name__, 'receipt': key})
            raise FireFlowMutationUnknown('FireFlow operation did not complete locally; outcome may be '
                             'unknown. Inspect ticket and receipt; do not retry automatically.') from None

    def add_comment(self, change_request_id, text, operation_id, reason):
        """Mirror one internal comment, then require its unique marker in history."""
        identifier = ticket_id(change_request_id)
        self._rt_intent(identifier, operation_id, reason)
        text = _text(text, 'comment text')
        if not text.strip() or any(ord(char) < 32 and char not in '\r\n\t' for char in text):
            raise ValueError('Invalid comment text')
        marker = '[jira-fireflow-bus:' + operation_id + ']'
        # RT treats space-prefixed lines as field continuation, including blank lines.
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        content = marker + '\n' + normalized
        serialized = content.replace('\n', '\n ')
        payload = {'content': 'id: ' + str(identifier) + '\nAction: comment\nText: '
                   + serialized + '\n'}

        def before():
            return {record['id'] for record in _rt_records(self.rt_history(identifier))}

        def verify(baseline):
            history = self.rt_history(identifier)
            if not any(record.get('id') not in baseline
                       and re.fullmatch(r'[1-9][0-9]{0,19}', record.get('id', ''))
                       and record.get('Type') == 'Comment'
                       and marker in record.get('Content', '')
                       for record in _rt_records(history)):
                raise ValueError('FireFlow comment was not found in history')
            return {'comment_marker': marker, 'history_sha256': digest(history)}

        return self._rt_mutate(identifier, operation_id, reason, 'fireflow_comment',
                               payload, '/comment', verify, before=before)

    def set_status(self, change_request_id, status, operation_id, reason):
        """Apply an explicitly mapped RT status and verify it through the modern API."""
        identifier = ticket_id(change_request_id)
        self._rt_intent(identifier, operation_id, reason)
        if (not isinstance(status, str) or not RT_STATUS.fullmatch(status)
                or status != status.strip()):
            raise ValueError('Invalid FireFlow status')
        payload = {'content': 'id: ticket/' + str(identifier) + '\nStatus: ' + status + '\n'}

        def before():
            return {record['id'] for record in _rt_records(self.rt_history(identifier))}

        def verify(baseline):
            history = self.rt_history(identifier)
            matches = [record for record in _rt_records(history)
                       if record['id'] not in baseline
                       and re.fullmatch(r'[1-9][0-9]{0,19}', record['id'])
                       and record.get('Type') == 'Set' and record.get('Field') == 'Status'
                       and record.get('NewValue') == status]
            if not matches:
                raise ValueError('FireFlow status change was not found in new history')
            result = self.get(identifier)
            fields = result['response']['data'].get('fields', [])
            values = [field.get('values') for field in fields
                      if isinstance(field, dict) and field.get('name') == 'status']
            if (len(values) != 1 or not isinstance(values[0], list)
                    or len(values[0]) != 1 or not isinstance(values[0][0], str)):
                raise ValueError('FireFlow current status unavailable')
            return {'status': status, 'current_status': values[0][0],
                    'transaction_id': matches[-1]['id'], 'history_sha256': digest(history),
                    'readback_sha256': result['sha256']}

        def noop(ticket):
            if ticket.get('Status') == status:
                return {'status': status, 'current_status': status, 'unchanged': True}
            return None

        return self._rt_mutate(identifier, operation_id, reason, 'fireflow_set_status',
                               payload, '/edit', verify, before=before, noop=noop)

    def _authorize(self, reason):
        if self.mode not in ('MANAGE', 'ROOT'):
            raise ValueError('READONLY denies FireFlow mutations')
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
            raise ValueError('Reason must have 3-500 characters')

    def _field_allowlist(self, fields):
        names = [field['name'].casefold() for field in fields]
        if len(names) != len(set(names)):
            raise ValueError('Duplicate field names')
        forbidden = {'status', 'validation', 'validation result', 'work order status',
                     'workflow'}
        if set(names) & forbidden:
            raise ValueError('Workflow/validation fields cannot be overwritten')
        allowed = self.config.get('allowed_fields', [])
        if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
            raise ValueError('Invalid field allowlist')
        allowed_names = {item.casefold() for item in allowed}
        if '*' in allowed_names:
            raise ValueError('Field allowlist must name fields explicitly')
        if set(names) - allowed_names:
            raise ValueError('Field not administrator-allowlisted')
        for field in fields:
            if field['name'].casefold() == 'devices':
                devices = self.config.get('allowed_devices', [])
                if (not isinstance(devices, list)
                        or any(not isinstance(item, str) for item in devices)):
                    raise ValueError('Invalid device allowlist')
                if '*' in devices:
                    raise ValueError('Device allowlist must name devices explicitly')
                if set(field['values']) - set(devices):
                    raise ValueError('Device not allowlisted')

    def _validate_fields(self, request):
        self._field_allowlist(request.fields)
        for line in request.traffic:
            self._field_allowlist(line['fields'])
            for group in ('source', 'destination', 'service', 'application', 'user'):
                for item in line[group]['items']:
                    self._field_allowlist(item['fields'])
        devices = [field for field in request.fields
                   if field['name'].casefold() == 'devices']
        if len(devices) != 1 or not devices[0]['values']:
            raise ValueError('Explicit devices required')

    def _receipt(self, path, data):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError('Unsafe operation receipt')
            with os.fdopen(descriptor, 'w') as stream:
                descriptor = -1
                json.dump(safe_response(data), stream, sort_keys=True,
                          separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        directory = os.open(self.state, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _mutate(self, operation_id, reason, payload):
        self._authorize(reason)
        if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
            raise ValueError('Invalid operation ID')
        origin = https_origin(self.config.get('base_url', ''))
        key = digest([origin, operation_id])
        receipt_keys = tuple(digest([candidate, operation_id])
                             for candidate in self.receipt_origins)
        entry = {
            'op': 'fireflow_create',
            'request_sha256': digest(payload),
            'reason_sha256': hashlib.sha256(reason.encode('utf-8')).hexdigest(),
            'traffic_lines': len(payload['traffic']),
        }
        self.audit.record(operation_id, 'received', entry)
        if any(receipt_exists(self.state / (candidate + suffix))
               for candidate in receipt_keys
               for suffix in ('.started.json', '.result.json')):
            raise ValueError(
                'Operation ID already used by a previous release; inspect receipt before any retry')
        try:
            self._receipt(self.state / (key + '.started.json'),
                          {'op': 'fireflow_create', 'operation_id': operation_id,
                           'request_sha256': digest(payload)})
        except FileExistsError:
            raise ValueError(
                'Operation ID already used; inspect receipt before any retry') from None
        try:
            self.audit.record(operation_id, 'authorized', entry)
            result = self._wire('/change-requests/traffic', method='POST', body=payload)
            self._receipt(self.state / (key + '.result.json'), result)
            self.audit.record(operation_id, 'completed', {**entry, 'receipt': key})
            return {'response': safe_response(result), 'operation_id': operation_id,
                    'receipt': key}
        except Exception as error:
            self.audit.record(operation_id, 'outcome_unknown',
                              {**entry, 'error_type': type(error).__name__,
                               'receipt': key})
            raise ValueError('FireFlow operation did not complete locally; outcome may be '
                             'unknown. Inspect ticket and receipt; do not retry automatically.') \
                from None

    def create(self, request, operation_id, reason):
        self._authorize(reason)
        validated = TrafficRequest.model_validate(request)
        templates = self.config.get('allowed_templates', [])
        if (not isinstance(templates, list)
                or any(not isinstance(item, str) for item in templates)
                or '*' in templates
                or validated.template not in templates):
            raise ValueError('Template not allowlisted')
        self._validate_fields(validated)
        payload = validated.model_dump()
        try:
            size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':'),
                                  allow_nan=False).encode('utf-8'))
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise ValueError('Invalid FireFlow request') from None
        if size > MAX_REQUEST_BYTES:
            raise ValueError('Request too large')

        # This GET both verifies the configured template and establishes a session before
        # the mutation receipt is claimed.  Failures here prove no create POST was sent.
        available = self.templates().get('data', [])
        if not isinstance(available, list) or not any(
                isinstance(item, dict) and item.get('name') == validated.template
                and item.get('enabled') is True and item.get('type') == 'Traffic Change'
                for item in available):
            raise ValueError('Traffic template unavailable to API account')
        return self._mutate(operation_id, reason, payload)
