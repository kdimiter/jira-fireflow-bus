"""Minimal Jira Cloud client: search, read, comment. Outbound HTTPS only.

The private side always initiates. Jira Cloud never reaches into the network, so there
are no webhooks and no inbound endpoint to defend — the bus polls.
"""
import base64
import json
import re
from urllib.parse import urlencode

from .config import resolve_secret
from .transport import https_origin, request_json

KEY = re.compile(r'[A-Z][A-Z0-9_]{0,20}-[0-9]{1,10}')
ISSUE_ID = re.compile(r'[1-9][0-9]{0,19}')


class JiraError(ValueError):
    """Safe to show: never carries the API token."""


class JiraMutationUnknown(JiraError):
    """A non-idempotent Jira request may have committed before its reply was lost."""


def validate_issue(value):
    """Validate the Jira search item used as the identity and mapping boundary."""
    if (not isinstance(value, dict)
            or not isinstance(value.get('id'), str)
            or not ISSUE_ID.fullmatch(value['id'])
            or not isinstance(value.get('key'), str)
            or not KEY.fullmatch(value['key'])
            or not isinstance(value.get('fields'), dict)):
        raise JiraError('Unexpected Jira issue in search response')
    return value


class Jira:
    def __init__(self, config, request=None):
        if not isinstance(config, dict):
            raise JiraError('Expected Jira configuration')
        self.config = dict(config)
        self.request = request or request_json
        for name in ('base_url', 'email', 'token_ref'):
            if not self.config.get(name):
                raise JiraError('Jira configuration needs ' + name)
        try:
            self.config['base_url'] = https_origin(self.config['base_url'])
        except ValueError as error:
            raise JiraError(str(error)) from None

    def _call(self, path, query=None, body=None, method='GET'):
        token = resolve_secret(self.config['token_ref'])
        credentials = base64.b64encode(('%s:%s' % (self.config['email'], token)).encode()).decode()
        transport = {k: v for k, v in self.config.items() if k in ('base_url', 'ca_file', 'tls_certificate_sha256')}
        try:
            return self.request(transport, path, query=query, body=body, method=method,
                                headers={'Authorization': 'Basic ' + credentials})
        except Exception as error:
            status = getattr(error, 'code', None)
            message = ('Jira request failed%s; check the token and the JQL'
                       % (' (HTTP %s)' % status if status else ''))
            # Jira POSTs add comments or advance workflow. If no definitive 4xx rejection
            # came back, replay could duplicate the comment or move the issue twice.
            if method == 'POST' and not (
                    isinstance(status, int) and 400 <= status < 500):
                raise JiraMutationUnknown(message) from None
            raise JiraError(message) from None

    def search(self, jql, fields, limit=50):
        """Read pages up to the configured scan ceiling.

        ``limit`` is the Jira page size. ``scan_limit`` bounds memory and work for one
        poll; an over-broad JQL fails closed instead of materializing an unbounded queue.
        """
        if not isinstance(jql, str) or not 1 <= len(jql) <= 2000:
            raise JiraError('Invalid JQL')
        scan_limit = self.config.get('scan_limit', 1000)
        if (isinstance(scan_limit, bool) or not isinstance(scan_limit, int)
                or not 1 <= scan_limit <= 10000):
            raise JiraError('jira.scan_limit must be a whole number from 1 to 10000')
        query = {'jql': jql, 'maxResults': max(1, min(int(limit), 100)),
                 'fields': ','.join(sorted(set(fields))) or 'summary'}
        issues, tokens = [], set()
        for _ in range(1000):
            reply = self._call('/rest/api/3/search/jql', query=query)
            page = reply.get('issues') if isinstance(reply, dict) else None
            if not isinstance(page, list):
                raise JiraError('Unexpected search response')
            if len(issues) + len(page) > scan_limit:
                raise JiraError('Jira search exceeds scan_limit; narrow the JQL')
            issues.extend(validate_issue(issue) for issue in page)
            token = reply.get('nextPageToken')
            if reply.get('isLast') is True or (not token and reply.get('isLast') is not False):
                return issues
            if not isinstance(token, str) or not token or token in tokens:
                raise JiraError('Invalid or repeated Jira pagination token')
            tokens.add(token)
            query['nextPageToken'] = token
        raise JiraError('Jira search exceeds 1000 pages; narrow the JQL')

    def myself(self):
        """Who the bus is authenticating as. The cheapest proof that the token works."""
        return self._call('/rest/api/3/myself')

    def read_issue(self, identifier, fields):
        """Read by immutable ID so moving an issue cannot transfer its approval."""
        if not isinstance(identifier, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', identifier):
            raise JiraError('Invalid immutable issue ID')
        if not isinstance(fields, (set, list, tuple)) or any(
                not isinstance(field, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', field)
                for field in fields):
            raise JiraError('Invalid issue fields')
        return self._call('/rest/api/3/issue/' + identifier,
                          query={'fields': ','.join(sorted(set(fields)))})

    def fields(self):
        """Every field this tenant defines, so nobody has to guess a customfield id.

        Each entry carries `id` ("customfield_10000"), `name` (what a person sees on the
        screen), `custom` and `schema`. Matching the human name to the id is the whole job,
        and it is the one thing that cannot be worked out from outside Jira.
        """
        reply = self._call('/rest/api/3/field')
        if not isinstance(reply, list):
            raise JiraError('Unexpected field list response')
        return reply

    def transitions(self, key):
        """The transitions this issue currently offers, by name."""
        if not KEY.fullmatch(key or ''):
            raise JiraError('Invalid issue key')
        reply = self._call('/rest/api/3/issue/%s/transitions' % key)
        return [str(item.get('name') or '') for item in (reply or {}).get('transitions') or []]

    def update_fields(self, key, fields):
        """Set configured text result fields; replaying the same values is safe."""
        if not KEY.fullmatch(key or ''):
            raise JiraError('Invalid issue key')
        if not isinstance(fields, dict) or not fields or any(
            not re.fullmatch(r'customfield_[0-9]+', name) or
            (value is not None and not isinstance(value, str))
            for name, value in fields.items()):
            raise JiraError('Result fields must be custom text fields')
        return self._call('/rest/api/3/issue/%s' % key,
                          body={'fields': fields}, method='PUT')

    def comment(self, key, text):
        if not KEY.fullmatch(key or ''):
            raise JiraError('Invalid issue key')
        body = {'body': {'type': 'doc', 'version': 1,
                         'content': [{'type': 'paragraph',
                                      'content': [{'type': 'text', 'text': str(text)[:2000]}]}]}}
        return self._call('/rest/api/3/issue/%s/comment' % key, body=body, method='POST')


    def transition(self, key, name):
        """Move the issue, if the board offers a transition with that name."""
        if not KEY.fullmatch(key or ''):
            raise JiraError('Invalid issue key')
        available = self._call('/rest/api/3/issue/%s/transitions' % key)
        for item in (available or {}).get('transitions') or []:
            if str(item.get('name', '')).lower() == str(name).lower():
                return self._call('/rest/api/3/issue/%s/transitions' % key,
                                  body={'transition': {'id': item['id']}}, method='POST')
        raise JiraError('No transition named %r on this issue' % str(name)[:40])


def plain(value):
    """Jira returns rich text as a document tree; take the text and nothing else."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if 'value' in value and isinstance(value['value'], str):
            return value['value'].strip()
        if 'name' in value and isinstance(value['name'], str):
            return value['name'].strip()
        if 'content' in value:
            return ' '.join(plain(item) for item in value.get('content') or []).strip()
        if 'text' in value and isinstance(value['text'], str):
            return value['text'].strip()
        return ''
    if isinstance(value, list):
        return ' '.join(plain(item) for item in value).strip()
    return str(value).strip()
