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
COMMENT_ORIGIN_PROPERTY = 'algosec-jira-bus.origin'


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

    def _issue_tail(self, identifier, resource, collection, limit, max_items,
                    after_id=None, query=None, newest_first=False):
        """Return a bounded recent tail with overlap at the durable event cursor."""
        if not isinstance(identifier, str) or not ISSUE_ID.fullmatch(identifier):
            raise JiraError('Invalid immutable issue ID')
        if after_id is not None and (not isinstance(after_id, str)
                                     or not ISSUE_ID.fullmatch(after_id)):
            raise JiraError('Invalid Jira event cursor')
        for name, value, ceiling in (('limit', limit, 100), ('max_items', max_items, 10000)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise JiraError('%s must be a whole number from 1 to %s' % (name, ceiling))
        result, seen = [], set()
        start, total = 0, None
        for _ in range(1000):
            parameters = dict(query or {}, startAt=start,
                              maxResults=min(limit, max_items - len(result)))
            reply = self._call('/rest/api/3/issue/%s/%s' % (identifier, resource),
                               query=parameters)
            page = reply.get(collection) if isinstance(reply, dict) else None
            if (not isinstance(page, list) or type(reply.get('startAt')) is not int
                    or reply['startAt'] != start or type(reply.get('total')) is not int
                    or reply['total'] < 0):
                raise JiraError('Unexpected Jira %s pagination' % resource)
            if total is None:
                total = reply['total']
                # Changelog is oldest-first. Start at the bounded tail after learning total.
                if not newest_first and total > max_items and start == 0:
                    start = total - max_items
                    result, seen = [], set()
                    continue
            elif reply['total'] != total:
                raise JiraError('Jira %s changed during pagination' % resource)
            end = start + len(page)
            last = reply.get('isLast')
            if (end > total or (not page and end < total)
                    or (last is not None and type(last) is not bool)
                    or (last is True and end != total) or (last is False and end >= total)):
                raise JiraError('Incomplete Jira %s pagination' % resource)
            for item in page:
                if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                        or not ISSUE_ID.fullmatch(item['id']) or item['id'] in seen):
                    raise JiraError('Invalid or repeated Jira %s event' % resource)
                seen.add(item['id'])
                result.append(item)
            found_cursor = after_id is not None and after_id in seen
            # Jira comments may be deleted. A lower surviving numeric id proves
            # overlap with the durable high-water mark without requiring that the
            # exact comment still exist. A complete collection within the bound is
            # also sufficient; the caller will discard ids at or below high-water.
            older_overlap = (after_id is not None
                             and any(int(value) < int(after_id) for value in seen))
            cursor_covered = found_cursor or older_overlap
            # A newest-first comment page already contains every event newer than
            # the cursor. Changelog pages are oldest-first, so finding the cursor
            # merely starts the interesting range: newer pages must still be read.
            if (newest_first and cursor_covered) or end == total or len(result) == max_items:
                complete_collection = start == 0 and end == total and total <= max_items
                if (after_id is not None and not cursor_covered
                        and not complete_collection):
                    raise JiraError('Jira %s cursor fell outside the bounded event tail'
                                    % resource)
                return sorted(result, key=lambda item: int(item['id']))
            start = end
        raise JiraError('Jira %s exceeds 1000 pages' % resource)

    def comments(self, identifier, after_id=None, limit=100, max_items=1000):
        """Read comments by immutable issue ID, with properties and rendered text."""
        comments = self._issue_tail(identifier, 'comment', 'comments', limit, max_items,
                                    after_id, {'orderBy': '-created'}, newest_first=True)
        # Properties expansion is documented on bulk comment reads, not GET comments.
        # Without it, an outbound bus comment could appear to be a new human comment.
        # Always hydrate from the endpoint that actually promises property expansion.
        # Some Jira responses include an empty ``properties`` member when properties
        # were not expanded; trusting that placeholder would echo our own comments.
        missing = [item['id'] for item in comments]
        properties = {}
        for start in range(0, len(missing), 100):
            identifiers = missing[start:start + 100]
            reply = self._call('/rest/api/3/comment/list', query={'expand': 'properties'},
                               body={'ids': [int(value) for value in identifiers]}, method='POST')
            values = reply.get('values') if isinstance(reply, dict) else None
            if (not isinstance(values, list) or len(values) != len(identifiers)
                    or reply.get('startAt') != 0 or reply.get('total') != len(identifiers)
                    or reply.get('isLast') is False):
                raise JiraError('Incomplete Jira comment properties response')
            returned = set()
            for item in values:
                if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                        or item['id'] not in identifiers or item['id'] in returned):
                    raise JiraError('Unexpected Jira comment properties response')
                returned.add(item['id'])
                properties[item['id']] = item.get('properties', [])
        result = []
        for item in comments:
            if item['id'] in properties:
                item = dict(item, properties=properties[item['id']])
            if (not isinstance(item.get('created'), str) or not item['created']
                    or not isinstance(item.get('author', {}), dict)
                    or not isinstance(item.get('body'), (str, dict))
                    or not isinstance(item.get('properties', []), list)
                    or any(not isinstance(prop, dict) or not isinstance(prop.get('key'), str)
                           for prop in item.get('properties', []))):
                raise JiraError('Unexpected Jira comment event')
            result.append(dict(item, text=plain(item['body'])))
        return result

    def changelog(self, identifier, after_id=None, limit=100, max_items=1000):
        """Read chronological status changes, bounded by all changelog entries scanned."""
        histories = self._issue_tail(identifier, 'changelog', 'values', limit, max_items,
                                     after_id)
        result = []
        for history in histories:
            items = history.get('items')
            if (not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
                    or not isinstance(history.get('created'), str) or not history['created']
                    or not isinstance(history.get('author', {}), dict)):
                raise JiraError('Unexpected Jira changelog event')
            changes = [item for item in items
                       if item.get('fieldId') == 'status' or item.get('field') == 'status']
            if len(changes) > 1:
                raise JiraError('Repeated status field in Jira changelog event')
            if not changes:
                event = {'id': history['id'], 'created': history['created'],
                         'author': history.get('author', {}), 'kind': 'other'}
                if 'historyMetadata' in history:
                    if not isinstance(history['historyMetadata'], dict):
                        raise JiraError('Unexpected Jira history metadata')
                    event['historyMetadata'] = history['historyMetadata']
                result.append(event)
            for item in changes:
                if (not isinstance(item.get('toString'), str) or not item['toString']
                        or any(item.get(key) is not None and not isinstance(item[key], str)
                               for key in ('fromString', 'from', 'to'))):
                    raise JiraError('Unexpected Jira status change')
                event = {'id': history['id'], 'created': history['created'], 'kind': 'status',
                         'author': history.get('author', {}),
                         'from': item.get('fromString') or '', 'to': item['toString'],
                         'from_id': item.get('from') or '', 'to_id': item.get('to') or ''}
                if 'historyMetadata' in history:
                    if not isinstance(history['historyMetadata'], dict):
                        raise JiraError('Unexpected Jira history metadata')
                    event['historyMetadata'] = history['historyMetadata']
                result.append(event)
        return result

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
        body['properties'] = [{'key': COMMENT_ORIGIN_PROPERTY, 'value': {'source': 'fireflow'}}]
        return self._call('/rest/api/3/issue/%s/comment' % key, body=body, method='POST')


    def transition(self, key, name):
        """Move the issue, if the board offers a transition with that name."""
        if not KEY.fullmatch(key or ''):
            raise JiraError('Invalid issue key')
        available = self._call('/rest/api/3/issue/%s/transitions' % key)
        for item in (available or {}).get('transitions') or []:
            if str(item.get('name', '')).lower() == str(name).lower():
                return self._call('/rest/api/3/issue/%s/transitions' % key,
                                  body={'transition': {'id': item['id']},
                                        'historyMetadata': {
                                            'generator': {'id': 'algosec-jira-bus', 'type': 'integration'},
                                            'cause': {'id': 'fireflow', 'type': 'status-sync'},
                                            'extraData': {COMMENT_ORIGIN_PROPERTY: 'fireflow'}}},
                                  method='POST')
        raise JiraError('No transition named %r on this issue' % str(name)[:40])


def plain(value):
    """Jira returns rich text as a document tree; take the text and nothing else."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if value.get('type') == 'doc':
            return _adf_text(value).strip()
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


def _adf_text(node):
    """Keep inline text exact and use newlines between document blocks."""
    if not isinstance(node, dict):
        return ''
    kind = node.get('type')
    if kind == 'text':
        return node.get('text') if isinstance(node.get('text'), str) else ''
    if kind == 'hardBreak':
        return '\n'
    if kind == 'mention':
        return str((node.get('attrs') or {}).get('text') or '')
    content = node.get('content')
    if not isinstance(content, list):
        return ''
    text = ''.join(_adf_text(child) for child in content)
    if kind in ('paragraph', 'heading', 'codeBlock', 'listItem', 'blockquote', 'tableRow'):
        return text.rstrip('\n') + '\n'
    return text
