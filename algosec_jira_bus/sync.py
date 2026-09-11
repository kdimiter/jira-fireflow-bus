"""A bus between Jira Cloud and FireFlow, driven entirely from the private side.

Two directions, one poll loop, one state file:

* **Jira to FireFlow** — an approved issue asking to open or close network access becomes
  one traffic change request. The Jira key travels with it, so the same issue can never
  produce a second request.
* **FireFlow to Jira** — as a request moves through the workflow, its new status is
  mirrored back to the issue as a comment, and optionally as a Jira transition.

A word on FireFlow's vocabulary, because conflating the two costs a release. A *stage* is
the box in the lifecycle diagram (Request, Plan, Approve, Review, Implement, Validate,
Match, Resolved, Audit). A *status* is the actual state of the request, it is what the API
returns, several statuses can share one stage, and an administrator can add, rename or
disable statuses per workflow. So the bus matches on statuses, treats their names as facts
about one deployment rather than about FireFlow, and compares them without regard to case.

Nothing is exposed: Jira Cloud is only ever read and written outbound. The bus only
*requests* a change and *reports* progress. It never approves, plans or implements one —
those stay with the FireFlow workflow and the people in it, which is the whole reason a
change process exists.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .adf import read_table
from .jira import Jira, JiraError, JiraMutationUnknown, plain, validate_issue
from .journal import Silent
from .queue import Failures, sent_anything
from .config import private_json
from .runtime import secure_dir
from .fireflow import TrafficRequest
from .transport import https_origin

# How many requests one pass may submit. Not a limit on what the bus *sees* -- the search
# reads every page, and anything over the cap is deferred to the next pass minutes later,
# never dropped. It exists because `limit` stopped being a safety valve when pagination
# arrived: a mis-scoped JQL used to submit fifty change requests and now would submit every
# issue it matched, all at once, on the first run somebody remembers to add --apply.
# Set it to 0 or null to lift the cap deliberately.
DEFAULT_MAX_PER_PASS = 50

ACTIONS = ('Allow', 'Drop')
VALUE = re.compile(r'[A-Za-z0-9_.:*/@ -]{1,256}')
REQUESTOR_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")

# FireFlow's own bound on one request, reported as NUMBER_OF_TRAFFIC_LINES_OUT_OF_BOUNDS.
# Refusing here gives one clear sentence naming the issue instead of a rejected
# submission whose operation id has already been spent.
MAX_TRAFFIC_LINES = 100

# A source or destination item is either a literal address or the name of an object
# already defined on the device, and FireFlow spells the two differently:
# AddressTrafficItemDetails carries `address`, NameTrafficItemDetails carries `name`.
# An object name put in the address field is rejected by the appliance, so which one a
# value is has to be decided rather than assumed -- this used to send everything as an
# address, which worked only for deployments that never name their objects.
IPV4 = r'(?:\d{1,3}\.){3}\d{1,3}'
IPV6 = r'[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}'
ADDRESS = re.compile(r'^(?:\*|%s(?:/\d{1,2})?|%s\s*-\s*%s|%s(?:/\d{1,3})?|%s\s*-\s*%s)$'
                    % (IPV4, IPV4, IPV4, IPV6, IPV6, IPV6))


def endpoint(value):
    """One source or destination item, in whichever shape FireFlow expects for it."""
    text = value.strip()
    return {'address': text} if ADDRESS.match(text) else {'name': text}


class MappingError(ValueError):
    """The issue does not describe a change this connector is willing to submit."""


def validate_for_adapter(request):
    """Use the bus FireFlow schema during preflight and dry run too."""
    try:
        TrafficRequest.model_validate(request)
    except ValueError as error:
        details = error.errors() if callable(getattr(error, 'errors', None)) else []
        locations = sorted({'.'.join(map(str, item.get('loc') or ())) for item in details})
        locations = [location for location in locations if location] or ['request']
        raise MappingError('Bus FireFlow adapter rejects request at %s; '
                           'check supported item types and limits' % ', '.join(locations[:5])) from None


def split(text):
    """Comma or newline separated list, as a human would type it into a Jira field."""
    return [item.strip() for item in re.split(r'[,\n;]+', text or '') if item.strip()]


def read_field(issue, spec):
    fields = issue.get('fields') or {}
    if isinstance(spec, dict) and spec.get('field'):
        raw = plain(fields.get(spec['field']))
        mapped = spec.get('values')
        if mapped:
            found = mapped.get(raw) or mapped.get(raw.lower()) or mapped.get(raw.title())
            if not found:
                raise MappingError('Unmapped value in %s: %r' % (spec['field'], raw[:40]))
            return found
        return raw
    if isinstance(spec, dict) and spec.get('constant'):
        return spec['constant']
    raise MappingError('Mapping entry needs field or constant')


def values(issue, spec, label):
    items = split(read_field(issue, spec))
    if not items:
        raise MappingError('No %s in the issue' % label)
    for item in items:
        if not VALUE.fullmatch(item):
            raise MappingError('Unusable %s value: %r' % (label, item[:40]))
    return items


def mapped_fields(mapping):
    """Every Jira field this mapping reads.

    A table mapping that leaves out `field` still reads the description, and forgetting
    that here would mean the search never asks for it and the parse finds no table -- a
    misconfiguration that looks exactly like an empty queue.
    """
    fields = set()
    for name, spec in (mapping or {}).items():
        if not isinstance(spec, dict):
            continue
        if name == 'table':
            fields.add(spec.get('field') or 'description')
        elif spec.get('field'):
            fields.add(spec['field'])
    return fields


def checked(items, label, where=''):
    """Reject a value that has no business reaching a firewall, and say which one."""
    if not items:
        raise MappingError('No %s%s' % (label, where))
    for item in items:
        if not VALUE.fullmatch(item):
            raise MappingError('Unusable %s%s: %r' % (label, where, item[:40]))
    return items


def line_of(source, destination, service, action, where=''):
    """One validated traffic line."""
    if action not in ACTIONS:
        raise MappingError('Action must be Allow or Drop%s, got %r'
                           % (where, str(action)[:40]))
    return {'source': {'items': [endpoint(i) for i in checked(source, 'source', where)]},
            'destination': {'items': [endpoint(i)
                                      for i in checked(destination, 'destination', where)]},
            'service': {'items': [{'service': i}
                                  for i in checked(service, 'service', where)]},
            'action': action}


def table_lines(issue, spec, fallback_action):
    """Traffic lines read from a table in a rich-text field, one line per row.

    A column the table does not carry falls back to the single-value mapping. That is how
    somebody puts the action in a custom field once instead of repeating it on every row.
    """
    columns = spec.get('columns') or {}
    if not columns:
        raise MappingError('A table mapping needs a "columns" map of name to heading')
    field = spec.get('field') or 'description'
    parsed, _found = read_table((issue.get('fields') or {}).get(field), columns,
                                minimum=spec.get('minimum_columns', 2))
    if not parsed:
        raise MappingError('No table with the expected headings (%s) in %r'
                           % (', '.join(sorted(columns.values())), field))
    lines = []
    for number, row in enumerate(parsed, start=1):
        where = ' in table row %d' % number
        action = (row.get('action') or '').strip().title() or fallback_action
        if not action:
            raise MappingError('No action%s and none mapped from a field' % where)
        lines.append(line_of(split(row.get('source', '')), split(row.get('destination', '')),
                             split(row.get('service', '')), action, where))
    return lines


def jira_attribution(issue, origin):
    """Human attribution, separate from the service identity used for API calls."""
    try:
        base = https_origin(origin)
    except ValueError:
        raise MappingError('Invalid Jira origin for attribution')
    key = str(issue.get('key', ''))
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*-[0-9]+', key):
        raise MappingError('Invalid Jira issue key for attribution')
    def clean(value):
        return ' '.join(str(value or '').split())[:256]
    lines = ['Jira request origin', 'Issue: ' + key, 'URL: ' + base + '/browse/' + key]
    fields = issue.get('fields') or {}
    for label, name in [('Creator', 'creator'), ('Reporter', 'reporter')]:
        person = fields.get(name)
        person = person if isinstance(person, dict) else {}
        lines.append(label + ': ' + (clean(person.get('displayName')) or 'Not available from Jira'))
        if person.get('emailAddress'):
            lines.append(label + ' email: ' + clean(person['emailAddress']))
    return '\n'.join(lines)


def jira_creator_requestor(issue):
    """Return the Jira creator email used as the FireFlow Requestor identity."""
    fields = issue.get('fields') or {}
    creator = fields.get('creator')
    creator = creator if isinstance(creator, dict) else {}
    email = creator.get('emailAddress')
    if (not isinstance(email, str) or email != email.strip()
            or len(email) > 254
            or not REQUESTOR_EMAIL.fullmatch(email)):
        raise MappingError(
            'Jira creator email is unavailable or invalid; FireFlow Requestor '
            'cannot be attributed safely')
    return email


def build(issue, mapping, template, devices, jira_origin=None):
    """One issue to one traffic request, or a MappingError explaining why not.

    A request carries a *list* of traffic lines. They come either from four single-value
    fields (one line) or from a table in a rich-text field (one line per row), because Jira
    has no repeating group of custom fields and a table is how a person writes four lines.
    """
    key = issue.get('key')
    if not key:
        raise MappingError('Issue without a key')
    requestor = jira_creator_requestor(issue)
    action = read_field(issue, mapping['action']) if mapping.get('action') else None
    if action is not None and action not in ACTIONS:
        raise MappingError('Action must map to Allow or Drop, got %r' % action)

    domain = None
    if mapping.get('structured'):
        from .structured import normalize
        spec = mapping['structured']
        try:
            domain = normalize((issue.get('fields') or {}).get(spec['field']))
        except (ValueError, KeyError, TypeError) as error:
            raise MappingError('Invalid structured request: %s' % error) from None
        traffic = []
        for row in domain['lines']:
            if any(row[side]['kind'] == 'hostname' for side in ('source', 'destination')):
                raise MappingError('Hostname transport is not verified for the installed adapter')
            traffic.append(line_of([row['source']['value']], [row['destination']['value']],
                                   row['services'], domain['action']))
    elif mapping.get('table'):
        traffic = table_lines(issue, mapping['table'], action)
    else:
        traffic = [line_of(values(issue, mapping['source'], 'source'),
                           values(issue, mapping['destination'], 'destination'),
                           values(issue, mapping['service'], 'service'), action)]
    if len(traffic) > MAX_TRAFFIC_LINES:
        raise MappingError('FireFlow takes at most %d traffic lines and this issue asks '
                           'for %d; split it' % (MAX_TRAFFIC_LINES, len(traffic)))

    summary = plain((issue.get('fields') or {}).get('summary'))[:200]
    request = {'template': template,
                 'fields': [{'name': 'subject', 'values': ['%s: %s' % (key, summary)]},
                            {'name': 'Requestor', 'values': [requestor]},
                            {'name': 'devices', 'values': list(devices)}],
                 'traffic': traffic}
    description = domain['justification'] if domain else ''
    if jira_origin:
        description = description + ('\n\n' if description else '') + jira_attribution(issue, jira_origin)
    if description:
        request['fields'].append({'name': 'Change Request Description', 'values': [description]})
    return key, request

class State:
    """Which issues already produced a request, so a restart cannot duplicate work.

    The failure queue shares this file (see ``queue.Failures``): one atomic replace covers
    both, so no crash can leave an issue recorded as created with its failure still queued.
    """

    def __init__(self, path):
        self.path = Path(path)
        secure_dir(self.path.parent)
        try:
            self.data = private_json(self.path, os.getuid(), max_bytes=16 * 1024 * 1024)
        except FileNotFoundError:
            self.data = {}
        if not isinstance(self.data, dict):
            raise ValueError('Invalid bus state: expected an object')
        self.data.setdefault('issues', {})
        self.data.setdefault('failures', {})
        self.data.setdefault('intents', {})
        if not all(isinstance(self.data[name], dict) for name in ('issues', 'failures', 'intents')):
            raise ValueError('Invalid bus state: issues and failures must be objects')

    @staticmethod
    @contextmanager
    def lock(path):
        """Hold across loading, external calls and saving, not merely the final replace.

        Every CLI writer uses this same lock; a blocked writer loads fresh state only
        after the previous process finishes. Process exit releases flock automatically.
        """
        path = Path(path).absolute()
        secure_dir(path.parent)
        fd = os.open(path.with_name(path.name + '.lock'),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                     getattr(os, 'O_CLOEXEC', 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise ValueError('State lock must be a private regular file')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def seen(self, key):
        return key in self.data['issues']

    def entries(self):
        return dict(self.data['issues'])

    def save(self):
        """Replace the file atomically, owner-readable only."""
        fd, temporary = tempfile.mkstemp(prefix='.' + self.path.name + '.', dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as stream:
                fd = None
                json.dump(self.data, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY |
                                   getattr(os, 'O_DIRECTORY', 0) |
                                   getattr(os, 'O_CLOEXEC', 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def record(self, key, outcome, clear_failure=False):
        current = dict(self.data['issues'].get(key) or {})
        current.update(outcome)
        self.data['issues'][key] = current
        if clear_failure:
            self.data['failures'].pop(key, None)
        self.save()


def intake_mode(settings):
    """Opt in to FireFlow-owned approval; existing deployments keep their gate."""
    config = settings.get('intake', {})
    if not isinstance(config, dict) or set(config) - {'mode'}:
        raise MappingError('intake must be an object containing only mode')
    mode = config.get('mode', 'approval')
    if mode not in ('approval', 'todo'):
        raise MappingError('intake.mode must be approval or todo')
    if mode == 'todo' and not (settings.get('mapping') or {}).get('structured'):
        raise MappingError('To Do intake requires a structured request mapping')
    return mode


def fresh_todo(jira, issue, wanted):
    """Map the fresh To Do snapshot, never the possibly stale search contents."""
    from .approval_gate import issue_id
    try:
        identifier = issue_id(issue.get('id'))
    except ValueError as error:
        raise MappingError(str(error)) from None
    fresh = jira.read_issue(identifier, wanted | {'summary', 'status'})
    if not isinstance(fresh, dict) or fresh.get('id') != identifier:
        raise MappingError('Jira returned a different immutable issue ID')
    status = (fresh.get('fields') or {}).get('status') or {}
    if not isinstance(status, dict) or status.get('name') != 'To Do':
        raise MappingError('Fresh Jira status must be To Do')
    return fresh


def run(settings, fireflow, state, jira=None, dry_run=True, log=print,
        failures=None, journal=None):
    """Poll once. Returns what was created, skipped, refused, deferred and parked.

    A failure on one issue never ends the pass and never disappears: it goes to the queue,
    which decides whether it may be tried again. See ``queue`` for why a change whose
    outcome is unknown is not one of those.
    """
    jira = jira or Jira(settings['jira'])
    journal = journal or Silent()
    failures = failures if failures is not None else Failures(state)
    validate_mirror(settings.get('mirror') or {})
    mode = intake_mode(settings)
    mapping = settings['mapping']
    template = settings['fireflow']['template']
    devices = settings['fireflow']['devices']
    wanted = mapped_fields(mapping) | {'creator', 'reporter'}
    cap = settings.get('max_per_pass', DEFAULT_MAX_PER_PASS)
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
        raise MappingError('max_per_pass must be a non-negative whole number, or null for no cap')
    try:
        issues = jira.search(settings['jira']['jql'], wanted | {'summary'},
                             limit=settings['jira'].get('limit', 50))
    except Exception as error:
        # A search that fails is not a per-issue problem and has no key to file it under.
        # Record it and let the caller decide; the next poll retries the whole query.
        journal.write('error', None, stage='search', error=type(error).__name__)
        raise
    created, skipped, refused, deferred, capped, failed = [], [], [], [], [], []
    existing_issue_ids = {
        entry.get('jira_issue_id') for entry in state.data['issues'].values()
        if isinstance(entry, dict) and entry.get('jira_issue_id')
    }
    blocked_intent_issue_ids = {
        intent.get('jira_issue_id') for key, intent in state.data['intents'].items()
        if (isinstance(intent, dict) and intent.get('jira_issue_id')
            and failures.blocked(key))
    }
    for issue in issues:
        key = issue.get('key') if isinstance(issue, dict) else '?'
        try:
            issue = validate_issue(issue)
        except JiraError as error:
            key = key if isinstance(key, str) and key else '?'
            refused.append((key, str(error)))
            journal.write('refused', key, error=str(error))
            log('refused %s: %s' % (key, error))
            continue
        identifier = issue['id']
        already = identifier in existing_issue_ids
        if state.seen(key) or already:
            skipped.append(key)
            continue
        if failures.blocked(key) or identifier in blocked_intent_issue_ids:
            deferred.append(key)
            continue
        if cap and len(created) + len(capped) >= cap:
            # Held back, not discarded: the next pass picks it up. Counting refusals here
            # would be wrong -- a refused issue costs nothing and never reaches FireFlow.
            capped.append(key)
            continue
        try:
            if mode == 'todo':
                issue = fresh_todo(jira, issue, wanted)
            elif mapping.get('structured') and not dry_run:
                from .approval_gate import ApprovalLedger
                try:
                    issue = ApprovalLedger(state.path.parent / 'approvals').verify(
                        jira, issue.get('id'), mapping['structured']['field'], template, devices,
                        extra_fields=wanted)
                except ValueError as error:
                    raise MappingError('Approval verification failed: %s' % error) from None
            key, request = build(issue, mapping, template, devices, jira_origin=settings['jira'].get('base_url'))
            validate_for_adapter(request)
        except MappingError as error:
            # A bad issue is a permanent refusal, not a failure to retry: nothing about
            # polling again makes an unmapped value map.
            refused.append((key, str(error)))
            journal.write('refused', key, error=str(error))
            log('refused %s: %s' % (key, error))
            continue
        if dry_run:
            created.append((key, request))
            if identifier:
                existing_issue_ids.add(identifier)
            traffic_digest = hashlib.sha256(json.dumps(
                request['traffic'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            log('would create for %s: traffic_lines=%d digest=%s' %
                (key, len(request['traffic']), traffic_digest))
            continue
        # Prefer Jira's immutable numeric ID so renaming an issue key cannot create a
        # second request. Include the tenant when configured to avoid cross-tenant reuse.
        origin = settings['jira'].get('base_url', '')
        if not origin:
            operation_id = 'jira-id-' + identifier
        else:
            tenant = hashlib.sha256(https_origin(origin).encode()).hexdigest()[:12]
            operation_id = 'jira-%s-%s' % (tenant, identifier)
        # Separate intent is durable before the network call, but is not proof of creation.
        # Reconciliation can find structured receipts even if create raises or we crash.
        state.data['intents'][key] = {'operation_id': operation_id,
                                      'jira_issue_id': identifier}
        state.save()
        blocked_intent_issue_ids.add(identifier)
        try:
            result = fireflow.create(request, operation_id, 'Requested in Jira issue ' + key)
        except Exception as error:
            # The adapter's own receipt says whether anything left this host. Only when it
            # says nothing did may the bus ever come back to this issue.
            unknown = sent_anything(fireflow, operation_id)
            entry = failures.record(key, 'create', error, retryable=not unknown, slow=True)
            journal.write('parked' if entry['parked'] else 'error', key, stage='create',
                          error=type(error).__name__, reason=entry.get('reason'),
                          outcome='unknown' if unknown else 'not_sent')
            failed.append((key, type(error).__name__))
            log('failed to create for %s (%s); %s' % (
                key, type(error).__name__,
                'outcome unknown, parked for review' if unknown else 'nothing was sent'))
            continue
        failures.clear(key)
        identifier = change_request_id(result)
        outcome = {'request': result.get('receipt'), 'operation_id': result.get('operation_id') or operation_id,
                   'change_request_id': identifier, 'status': None, 'jira_issue_id': issue['id']}
        submitted = (settings.get('mirror') or {}).get('submitted_transition')
        if identifier is not None and submitted:
            outcome['pending_transition'] = submitted
            outcome['workflow_target'] = submitted
        id_field = (settings.get('mirror') or {}).get('result_fields', {}).get('id')
        if identifier is not None and id_field:
            outcome.update(pending_fields={id_field: str(identifier)},
                           pending_observation={'id': identifier, 'create_only': True})
        state.record(key, outcome)
        existing_issue_ids.add(issue['id'])
        created.append((key, result))
        journal.write('created', key, change_request_id=identifier, receipt=result.get('receipt'))
        if identifier is None:
            # The request exists -- the receipt proves it -- but nothing can be mirrored back
            # without its id, and that would otherwise be a silence rather than a problem.
            # Reconciliation reports it as 'id_missing'; say it here too, at the moment it
            # happens, because the first live run is when this needs to be noticed.
            log('created for %s but FireFlow returned no request id; nothing can be mirrored '
                'back until it is found (see reconcile)' % key)
        else:
            log('created for %s' % key)
    if capped:
        log('%d issue(s) held back by max_per_pass=%d; the next pass takes them: %s'
            % (len(capped), cap, ', '.join(capped[:5]) + ('...' if len(capped) > 5 else '')))
    journal.write('pass', None, phase='intake', created=len(created), skipped=len(skipped),
                  refused=len(refused), deferred=len(deferred), capped=len(capped),
                  dry_run=dry_run)
    return {'created': created, 'skipped': skipped, 'refused': refused, 'failed': failed,
            'deferred': deferred, 'capped': capped, 'parked': sorted(failures.parked())}


# FireFlow answers every REST call with the same envelope:
#
#     {"status": "Success"|"Failure", "messages": [...], "data": {...}}
#
# That top-level "status" is the outcome of the *call*. The status of the change request
# lives inside data.fields[], as {"name": "status", "values": ["implement"]}. Reading the
# envelope instead would mirror the word "Success" into the issue once and then never see
# a change again, which is exactly the shape of a bug that looks like it works.
ENVELOPE = ('status', 'messages')


def body_of(result):
    """The ticket itself, with FireFlow's envelope taken off.

    Returns None rather than guessing when the envelope is there but carries no ``data``:
    handing back the envelope would put its "Success" where a ticket field is expected.
    """
    response = (result or {}).get('response')
    if not isinstance(response, dict):
        return None
    data = response.get('data')
    if isinstance(data, dict):
        return data
    if all(name in response for name in ENVELOPE):
        return None
    # No envelope at all: a caller that already unwrapped it, or a release that stops
    # sending one. Then the response *is* the ticket.
    return response


def field_of(body, wanted):
    """One value out of the ticket's ``fields`` array, matched by name, ignoring case."""
    for field in (body or {}).get('fields') or []:
        if not isinstance(field, dict):
            continue
        if str(field.get('name') or '').strip().casefold() != wanted.casefold():
            continue
        values = field.get('values')
        text = plain(values[0] if isinstance(values, list) and values else values)
        if text:
            return text
    return None


# FireFlow change request ids are positive and fit a signed 32-bit integer -- the same
# range the FireFlow API client enforces before it will call anything with one. Anything
# outside it is not a small mistake to pass along: an id is the proof a request exists, and
# recovery leans on it to decide whether a change whose outcome was unknown actually
# happened. A zero or a negative number is not weak proof, it is none.
MIN_REQUEST_ID, MAX_REQUEST_ID = 1, 2147483647


def valid_request_id(value):
    """The value if it is a usable change request id, otherwise None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    return value if MIN_REQUEST_ID <= value <= MAX_REQUEST_ID else None


def change_request_id(result):
    """The created request's id, without assuming one response shape.

    Note that FireFlow's documented example of a *successful create* carries an empty
    ``data``, so this may legitimately find nothing. The caller must treat that as a fact
    to report, never as a failure to retry: the request was created either way.
    """
    body = body_of(result)
    if not isinstance(body, dict):
        return None
    for name in ('id', 'changeRequestId', 'change_request_id', 'ticketId'):
        identifier = valid_request_id(body.get(name))
        if identifier is not None:
            return identifier
    return valid_request_id(field_of(body, 'id'))


def status_of(ticket):
    """The change request's workflow status, or None if this reply does not carry one.

    Taken from ``data.fields[]`` where the name is "status" -- never from the envelope.
    """
    response = (ticket or {}).get('response')
    if isinstance(response, dict) and 'messages' in response and response.get('status') != 'Success':
        return None
    body = body_of(ticket)
    if body is None:
        return None
    status = field_of(body, 'status')
    if status:
        return status
    # Some shapes promote it out of the fields array. Accepting it *here* is safe: `body`
    # is the ticket, so this is the ticket's own status and not the envelope's verdict.
    for name in ('status', 'Status', 'currentStatus'):
        value = body.get(name)
        text = plain(value) if not isinstance(value, str) else value.strip()
        if text:
            return text
    return None


def same_status(one, other):
    """FireFlow's statuses are lower case in the API and title case in most people's heads."""
    return (one or '').strip().casefold() == (other or '').strip().casefold()


def transition_for(transitions, status):
    """The board transition configured for this status, matched without regard to case."""
    for name, target in (transitions or {}).items():
        if same_status(name, status):
            return target
    return None


def validate_mirror(config):
    if type(config.get('verify_resolved_children', False)) is not bool:
        raise MappingError('verify_resolved_children must be boolean')
    submitted = config.get('submitted_transition')
    if submitted is not None and (not isinstance(submitted, str) or not submitted.strip()):
        raise MappingError('mirror.submitted_transition must be a nonempty transition name')
    result = config.get('result_fields', {})
    if not isinstance(result, dict) or any(
        name not in ('id', 'status', 'owner') or not isinstance(field, str)
        or not re.fullmatch(r'customfield_[0-9]+', field) for name, field in result.items()):
        raise MappingError('result_fields maps id/status/owner to Jira custom text fields')
    if len(set(result.values())) != len(result):
        raise MappingError('Result field IDs must be distinct')
    rules = config.get('outcome_rules', [])
    if not isinstance(rules, list):
        raise MappingError('mirror.outcome_rules must be a list')
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or any(
            not isinstance(rule.get(name), str) or not rule[name].strip()
            for name in ('status', 'field', 'equals', 'transition')):
            raise MappingError('Outcome rules require nonempty status, field, equals and transition')
        identity = (rule['status'].strip().casefold(), rule['field'], rule['equals'])
        if identity in seen:
            raise MappingError('Duplicate outcome rule')
        seen.add(identity)
    fields = config.get('observed_fields', [])
    if not isinstance(fields, list) or any(not isinstance(f, str) or not f.strip() for f in fields):
        raise MappingError('mirror.observed_fields must be a list of field names')


def deliver_fields(key, payload, state, jira, failures):
    try:
        jira.update_fields(key, payload)
    except Exception as error:
        failures.record(key, 'fields', error)
        return False
    state.record(key, {'pending_fields': None, 'delivered_fields': payload}, clear_failure=True)
    return True


def result_fields(config, identifier, status, details):
    values = {'id': str(identifier), 'status': status, 'owner': details.get('Owner')}
    return {field: values[name] for name, field in config.get('result_fields', {}).items()}


def workflow_transition(config, status, details):
    """Deployment-specific outcome evidence takes precedence over status-only mapping.

    A status covered by outcome rules fails closed when its evidence is absent.
    Field names and values must be verified against the target workflow.
    """
    rules = [rule for rule in config.get('outcome_rules', [])
             if same_status(rule['status'], status)]
    if rules:
        matches = {rule['transition'] for rule in rules
                   if details.get(rule['field']) == rule['equals']}
        return next(iter(matches)) if len(matches) == 1 else None
    return transition_for(config.get('transitions', {}), status)


def move_pending(key, target, status, state, jira, failures, journal, log):
    """Finish the durable transition before observing another workflow status."""
    try:
        jira.transition(key, target)
    except Exception as error:
        queued = failures.record(key, 'transition', error,
                                 retryable=not isinstance(error, JiraMutationUnknown))
        journal.write('parked' if queued['parked'] else 'error', key,
                      stage='transition', target=target, error=type(error).__name__)
        log('could not move %s to %s: %s' % (key, target, type(error).__name__))
        return False
    state.record(key, {'pending_transition': None}, clear_failure=True)
    journal.write('transition', key, status=status, target=target)
    return True


def resolved_tree_verified(fireflow, ticket, identifier, seen=None):
    """Require terminal status and explicit validation success throughout the tree."""
    seen = set() if seen is None else seen
    if identifier in seen or len(seen) >= 100:
        return False
    seen.add(identifier)
    body = body_of(ticket)
    if not isinstance(body, dict):
        return False
    data = body.get('data', body)
    if str(data.get('id')) != str(identifier) or not same_status(status_of(ticket), 'resolved'):
        return False
    if field_of(body, 'Validation Result Details') != 'SUCCESS':
        return False
    children = data.get('subChangeRequests')
    if children is None:
        children = []
    if not isinstance(children, list):
        return False
    for child in children:
        if type(child) is not int or child <= 0:
            return False
        if not resolved_tree_verified(fireflow, fireflow.get(child), child, seen):
            return False
    return True


def mirror(settings, fireflow, state, jira=None, dry_run=True, log=print,
           failures=None, journal=None):
    """Deliver comments and durable board transitions independently.

    ``status`` means the last commented status, not a completed board transition.
    A pending transition is saved with that status and finished before reading another.
    A timeout after Jira accepts a POST can still require manual reconciliation;
    these separate writes cannot provide exactly-once delivery across two systems.
    """
    jira = jira or Jira(settings['jira'])
    journal = journal or Silent()
    failures = failures if failures is not None else Failures(state)
    validate_mirror(settings.get('mirror') or {})
    transitions = (settings.get('mirror') or {}).get('transitions') or {}
    template = (settings.get('mirror') or {}).get('comment', 'AlgoSec FireFlow request %(id)s is now %(status)s.')
    updated, unchanged, deferred, failed = [], [], [], []
    for key, entry in sorted(state.entries().items()):
        identifier = entry.get('change_request_id')
        if not identifier:
            continue
        if failures.blocked(key):
            deferred.append(key)
            continue
        jira_sync = entry.get('jira_sync')
        suppression = (jira_sync.get('mirror_suppression')
                       if isinstance(jira_sync, dict) else None)
        if suppression is not None and not isinstance(suppression, dict):
            suppression = None
        pending = entry.get('pending_fields')
        if pending:
            observation = entry.get('pending_observation')
            if observation and (settings.get('mirror') or {}).get('result_fields'):
                if observation.get('create_only'):
                    field = settings['mirror']['result_fields'].get('id')
                    if field:
                        pending = {field: str(observation['id'])}
                else:
                    pending = result_fields(settings['mirror'], observation['id'],
                                            observation['status'], observation['details'])
                if not dry_run:
                    state.record(key, {'pending_fields': pending})
            if dry_run:
                deferred.append(key)
                continue
            if not deliver_fields(key, pending, state, jira, failures):
                failed.append((key, 'fields'))
                continue
        target = entry.get('pending_transition')
        # Upgrade an existing queue written before pending transitions were persisted.
        if not target and (failures.entry(key) or {}).get('stage') == 'transition':
            target = transition_for(transitions, entry.get('status'))
            if not target:
                # An unfinished transition whose target the configuration no longer names,
                # because the mapping was edited between the failure and now. Reading on
                # would report a newer status and quietly bury the fact that this card is
                # still stuck half-way. Leave it queued and say so every pass until a
                # person restores the mapping or releases it.
                journal.write('error', key, stage='transition', error='UnmappedTarget',
                              status=entry.get('status'))
                failed.append((key, 'transition:unmapped'))
                log('%s has an unfinished transition for status %r and the configuration no '
                    'longer maps it; not reading a newer status until that is settled'
                    % (key, entry.get('status')))
                continue
        retried_transition = bool(target)
        if target:
            if dry_run:
                log('would retry transition for %s to %s' % (key, target))
                deferred.append(key)
                continue
            if not move_pending(key, target, entry.get('status'), state, jira,
                                failures, journal, log):
                failed.append((key, 'transition'))
                continue
        try:
            ticket = fireflow.get(identifier)
            status = status_of(ticket)
            mirror_config = settings.get('mirror') or {}
            names = {'Owner'} | set(mirror_config.get('observed_fields', []))
            names.update(rule['field'] for rule in mirror_config.get('outcome_rules', []))
            details = {name: field_of(body_of(ticket), name) for name in sorted(names)}
            details = {name: value for name, value in details.items() if value is not None}
            if mirror_config.get('verify_resolved_children') and same_status(status, 'resolved'):
                details['Completion verified'] = 'yes' if resolved_tree_verified(
                    fireflow, ticket, identifier) else 'no'
            if not status:
                raise ValueError('FireFlow response contains no workflow status')
        except Exception as error:
            if not dry_run:
                queued = failures.record(key, 'read', error)
                journal.write('parked' if queued['parked'] else 'error', key, stage='read',
                              error=type(error).__name__)
            failed.append((key, type(error).__name__))
            log('could not read %s: %s' % (key, type(error).__name__))
            continue
        payload = result_fields(mirror_config, identifier, status, details)
        if payload and payload != state.entries()[key].get('delivered_fields') and not dry_run:
            state.record(key, {'pending_fields': payload, 'pending_observation':
                               {'id': identifier, 'status': status, 'details': details}})
            if not deliver_fields(key, payload, state, jira, failures):
                failed.append((key, 'fields'))
                continue
        status_changed = not same_status(status, entry.get('status'))
        details_changed = details != entry.get('workflow_details', {})
        if not status_changed and not details_changed:
            target = workflow_transition(mirror_config, status, details)
            if suppression and not dry_run:
                cleared_sync = deepcopy(jira_sync)
                cleared_sync.pop('mirror_suppression', None)
                state.record(key, {'jira_sync': cleared_sync,
                                   'workflow_target': target,
                                   'pending_transition': None})
                unchanged.append(key)
                continue
            if target and target != entry.get('workflow_target') and not retried_transition:
                if dry_run:
                    log('would move %s to %s' % (key, target))
                    deferred.append(key)
                    continue
                state.record(key, {'workflow_target': target, 'pending_transition': target})
                if not move_pending(key, target, status, state, jira, failures, journal, log):
                    failed.append((key, 'transition'))
                    continue
                updated.append((key, status))
                continue
            if not dry_run and (failures.entry(key) or {}).get('stage') == 'read':
                failures.clear(key)
            unchanged.append(key)
            continue
        message = template % {'id': identifier, 'status': status, 'key': key}
        if details:
            message += '\n' + '\n'.join('%s: %s' % pair for pair in details.items())
        if dry_run:
            log('would tell %s: %s' % (key, message))
            updated.append((key, status))
            continue
        try:
            jira.comment(key, message)
        except Exception as error:
            queued = failures.record(key, 'comment', error,
                                     retryable=not isinstance(error, JiraMutationUnknown))
            failed.append((key, 'comment:' + type(error).__name__))
            journal.write('parked' if queued['parked'] else 'error', key, stage='comment',
                          error=type(error).__name__)
            log('could not comment on %s: %s' % (key, type(error).__name__))
            continue
        target = workflow_transition(mirror_config, status, details)
        if target == entry.get('workflow_target'):
            target = None
        full_target = workflow_transition(mirror_config, status, details)
        update = {'status': status, 'workflow_details': details,
                  'workflow_target': full_target, 'pending_transition': target}
        if suppression:
            cleared_sync = deepcopy(jira_sync)
            cleared_sync.pop('mirror_suppression', None)
            update['jira_sync'] = cleared_sync
            update['pending_transition'] = None
            target = None
        state.record(key, update, clear_failure=True)
        journal.write('mirrored', key, status=status, change_request_id=identifier)
        if target and not move_pending(key, target, status, state, jira, failures, journal, log):
            failed.append((key, 'transition'))
        updated.append((key, status))
        log('mirrored %s -> %s' % (key, status))
    journal.write('pass', None, phase='mirror', updated=len(updated), unchanged=len(unchanged),
                  deferred=len(deferred), failed=len(failed), dry_run=dry_run)
    return {'updated': updated, 'unchanged': unchanged, 'deferred': deferred,
            'failed': failed, 'parked': sorted(failures.parked())}
