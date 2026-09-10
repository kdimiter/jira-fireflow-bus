"""Find where the bus's own picture stopped matching reality, and repair only its own.

A poll loop drifts. A state file is restored from a backup a day old; a create succeeds and
the process is killed before the id is written down; a mutation starts and never finishes.
Left alone, each of these means an access request that exists somewhere but is never
mirrored, and nobody notices until somebody asks why their ticket went quiet.

**What this repairs and what it refuses to.** The FireFlow adapter's receipts are written
next to the change they describe, on this host, before and after the call. They are the one
account of what was submitted that no network problem can blur, so they are the source used
here. Repair is limited to the bus's own state file -- backfilling an id or an entry that
was lost. Nothing is created, commented, transitioned or closed: a divergence in Jira or in
FireFlow is reported to a person, because deciding what a divergence *means* is the part
that needs judgement.

One deliberate silence: this release of the adapter reports a change request that does not
exist and one it could not reach with the same error, so a read that fails is recorded as
``unreadable`` and never as missing. Saying a request vanished when the link was merely
down would be worse than saying nothing.
"""
import json

from .journal import Silent
from .queue import Failures, started_receipt
from .sync import change_request_id, same_status, status_of


def result_receipt(fireflow, operation_id):
    """Path of the adapter's post-execution receipt, the twin of ``started_receipt``."""
    started = started_receipt(fireflow, operation_id)
    return None if started is None else started.with_name(started.name.replace('.started.', '.result.'))


def operation_for(key):
    """The operation id the bus uses for a Jira key. Must match ``sync.run``."""
    return 'jira-' + key.replace('-', '_')


def _load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, AttributeError):
        return None


def receipts(fireflow, key, operation=None):
    """What the adapter's receipts say happened for this issue."""
    operation = operation or operation_for(key)
    started, result = started_receipt(fireflow, operation), result_receipt(fireflow, operation)
    began = bool(started and started.exists())
    body = _load(result) if result else None
    return {'began': began, 'finished': body is not None, 'response': body}


def reconcile(keys, fireflow, state, failures=None, heal=True, journal=None, log=print):
    """Compare state against the receipts, and against FireFlow where it is safe to ask.

    ``keys`` is every Jira key worth checking: the issues currently matching the intake
    query plus everything the state already knows about.
    """
    journal = journal or Silent()
    failures = failures if failures is not None else Failures(state)
    entries = state.entries()
    findings, healed = [], []

    intents = state.data.get('intents', {})
    known = set(keys) | set(entries) | set(failures.store) | set(intents)
    for key in sorted(known):
        entry = entries.get(key)
        identity = {**intents.get(key, {}), **(entry or {})}
        operation = identity.get('operation_id')
        if not operation and identity.get('jira_issue_id'):
            findings.append({'key': key, 'finding': 'identity_missing',
                             'detail': 'immutable issue identity has no saved operation id; cannot infer receipt from Jira key'})
            continue
        operation = operation or operation_for(key)
        seen = receipts(fireflow, key, operation)

        if seen['began'] and not seen['finished']:
            # The adapter started a change and never wrote a result. Whatever happened on
            # the appliance, this is precisely the case that must not be repeated.
            findings.append({'key': key, 'finding': 'unfinished',
                             'detail': 'a change was begun and never completed; inspect the ticket and the receipt'})
            if heal and not (failures.entry(key) or {}).get('parked'):
                failures.record(key, 'create', 'unfinished receipt found during reconciliation',
                                retryable=False)
                healed.append({'key': key, 'action': 'parked'})
            continue

        if seen['finished'] and entry is None:
            # A request exists for this issue and the state file does not know it. The
            # receipt already stops a duplicate; without this the request would simply
            # never be mirrored back to the person who asked for it.
            identifier = change_request_id({'response': seen['response']})
            findings.append({'key': key, 'finding': 'state_lost',
                             'detail': 'a receipt records a request this state file never had'})
            if heal:
                # Only a usable id is proof. `change_request_id` already refuses zero,
                # negatives and anything out of range, so `is not None` is the whole test
                # -- but write it that way rather than as a truth test, because a truthy
                # check let -1 through and that is what unblocked a change nobody had
                # confirmed.
                state.record(key, {'request': None, 'operation_id': operation,
                                   'jira_issue_id': identity.get('jira_issue_id'),
                                   'change_request_id': identifier, 'status': None},
                             clear_failure=identifier is not None and
                             (failures.entry(key) or {}).get('stage') == 'create')
                healed.append({'key': key, 'action': 'restored', 'change_request_id': identifier})
            continue

        if entry is not None and not entry.get('change_request_id'):
            identifier = change_request_id({'response': seen['response']}) if seen['finished'] else None
            findings.append({'key': key, 'finding': 'id_missing',
                             'detail': ('the request was created but its id was never recorded'
                                        if seen['finished'] else
                                        'this issue has no request id and no receipt to recover one from')})
            if heal and identifier is not None:
                state.record(key, {'change_request_id': identifier},
                             clear_failure=(failures.entry(key) or {}).get('stage') == 'create')
                healed.append({'key': key, 'action': 'id_restored', 'change_request_id': identifier})
            continue

        if entry is None:
            continue

        # A create parked as outcome_unknown, where the receipt now names the very request
        # the state file already holds. The outcome is no longer unknown: the two agree, so
        # the change happened and the park has nothing left to protect. Agreement is the
        # condition -- a receipt naming a *different* request is a discrepancy for a person,
        # never a release.
        queued = failures.entry(key) or {}
        if queued.get('stage') == 'create' and seen['finished']:
            confirmed = change_request_id({'response': seen['response']})
            if confirmed is not None and confirmed == entry.get('change_request_id'):
                findings.append({'key': key, 'finding': 'create_confirmed',
                                 'detail': 'a receipt confirms request %d, so the unknown '
                                           'outcome is settled' % confirmed})
                if heal:
                    failures.clear(key)
                    healed.append({'key': key, 'action': 'unparked',
                                   'change_request_id': confirmed})
            else:
                findings.append({'key': key, 'finding': 'create_disputed',
                                 'detail': 'the receipt names %r and the state file holds %r; '
                                           'they must agree before this is released'
                                           % (confirmed, entry.get('change_request_id'))})

        try:
            status = status_of(fireflow.get(entry['change_request_id']))
        except Exception as error:
            findings.append({'key': key, 'finding': 'unreadable',
                             'detail': 'FireFlow would not return this request (%s); it may be gone or merely unreachable'
                                       % type(error).__name__})
            continue
        if status and not same_status(status, entry.get('status')):
            # Not a fault: the next mirror pass reports it. Worth naming so an operator
            # reading the report can tell a lagging bus from a stopped one.
            findings.append({'key': key, 'finding': 'drift',
                             'detail': 'FireFlow is at %r, the issue was last told %r'
                                       % (status, entry.get('status'))})

    # Anything already named above -- including a change this pass has just parked -- is
    # not listed a second time: one problem, one finding, or the report teaches an operator
    # to skim it.
    reported = {finding['key'] for finding in findings}
    for key, parked in sorted(failures.parked().items()):
        if key in reported:
            continue
        findings.append({'key': key, 'finding': 'parked',
                         'detail': 'queued failure needs a person: %s' % parked.get('reason')})

    for finding in findings:
        log('%s: %s -- %s' % (finding['key'], finding['finding'], finding['detail']))
    journal.write('reconcile', None, checked=len(known),
                  findings=len(findings), healed=len(healed), heal=heal)
    return {'findings': findings, 'healed': healed}
