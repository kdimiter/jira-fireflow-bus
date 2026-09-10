"""Command-line entry point for the Jira <-> FireFlow bus.

Four things an operator needs from one process: poll, look at what got stuck, let a stuck
thing go once they have looked, and check the two systems still agree. Dry run is the
default for the one that changes anything: the first thing anybody should see is what the
bus *would* do with their real issues.
"""
from contextlib import contextmanager
from functools import partial
import argparse
import json
import os
from pathlib import Path
import time

from algosec_mcp.config import private_json
from algosec_mcp.fireflow import FireFlow
from algosec_mcp.http import request_json
from algosec_mcp.runtime import Audit

from .doctor import run as diagnose
from .jira import Jira
from .journal import Journal
from .queue import Failures
from .reconcile import reconcile
from .sync import State, mirror, run

STATE = Path.home() / '.local/state/algosec-host-mcp'


def locations(settings, state_dir=None):
    directory = Path(state_dir or settings.get('journal') or STATE).expanduser()
    # An explicit CLI override must move state, receipts and journal together.
    state = directory / 'jira-sync.json' if state_dir else Path(
        settings.get('state', directory / 'jira-sync.json')).expanduser()
    return directory, state


@contextmanager
def session(settings, state_dir=None):
    """Serialize poll, reconciliation and release before any state is loaded."""
    _, path = locations(settings, state_dir)
    with State.lock(path):
        yield build(settings, state_dir)


def build(settings, state_dir=None):
    """The four things every mode needs, all rooted in one private state directory."""
    directory, path = locations(settings, state_dir)
    try:
        audit = Audit(directory)
    except ValueError as error:
        # The connector guards this hard and says so without naming the path. The path is
        # the whole of what an operator needs here, so say it: this is the first thing a
        # fresh deployment gets wrong, and 'chmod 700' is the entire fix.
        raise SystemExit('%s: %s (needs mode 0700 and your own ownership)' % (error, directory))
    timeout = settings['fireflow'].get('request_timeout', 30)
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError('fireflow.request_timeout must be an integer from 1 to 600 seconds')
    fireflow = FireFlow(settings['fireflow'], 'MANAGE', audit,
                       request=partial(request_json, timeout=timeout))
    state = State(path)
    return fireflow, state, Failures(state), Journal(directory)


def unresolved(state):
    """Work this pass began and did not finish, which no error necessarily reported.

    Both of these are silences rather than failures, and both stop the bus doing its job
    for that issue: a transition still owed to a board leaves a card half-moved, and a
    request created without an id can never be mirrored back to the person who asked.
    """
    entries = state.entries()
    return ([key for key, entry in sorted(entries.items()) if entry.get('pending_transition') or entry.get('pending_fields')],
            [key for key, entry in sorted(entries.items())
             if entry.get('operation_id') and not entry.get('change_request_id')])


def once(settings, apply_changes=False, log=print, state_dir=None):
    with session(settings, state_dir) as (fireflow, state, failures, journal):
        intake = run(settings, fireflow, state, dry_run=not apply_changes, log=log,
                     failures=failures, journal=journal)
        back = mirror(settings, fireflow, state, dry_run=not apply_changes, log=log,
                      failures=failures, journal=journal)
        pending, missing = unresolved(state)
        return {'intake': intake, 'mirror': back, 'pending': pending, 'missing_ids': missing}


def apply_mode(settings, arguments):
    """Whether this pass writes. Configuration decides; the command line overrides it.

    apply lives in the configuration so that reinstalling or upgrading the service cannot
    quietly put a working deployment back into dry run -- the unit file is replaced on an
    upgrade, and a mode kept only in its ExecStart would go with it.
    """
    configured = settings.get('apply', False)
    if not isinstance(configured, bool):
        # 'false' is a string and every string is truthy. Guessing here would turn a typo
        # into a deployment that writes to two production systems.
        raise ValueError('apply must be true or false, not %r' % (configured,))
    if getattr(arguments, 'dry_run', False):
        return False
    return True if getattr(arguments, 'apply', False) else configured


# Anything in this list means the pass left work undone. Some are errors and some are
# silences; from the outside they are the same thing -- a reason not to report success.
UNFINISHED = (('intake', 'failed'), ('intake', 'refused'),
              ('mirror', 'failed'), ('mirror', 'parked'),
              (None, 'pending'), (None, 'missing_ids'))


def outstanding(summary):
    """The keys of everything the pass could not finish."""
    found = []
    for section, name in UNFINISHED:
        holder = summary.get(section) if section else summary
        found.extend((holder or {}).get(name) or [])
    return found


def check(settings, heal=True, log=print, state_dir=None):
    """A reconciliation pass. Jira is consulted if it answers, and skipped if it does not.

    The receipts that matter are on this host, so a reconciliation is still worth running
    when Jira is unreachable -- it simply checks fewer keys.
    """
    with session(settings, state_dir) as (fireflow, state, failures, journal):
        keys = []
        try:
            issues = Jira(settings['jira']).search(settings['jira']['jql'], {'summary'},
                                                   limit=settings['jira'].get('limit', 50))
            keys = [issue.get('key') for issue in issues if issue.get('key')]
        except Exception as error:
            log('Jira did not answer (%s); checking what this host knows' % type(error).__name__)
        return reconcile(keys, fireflow, state, failures=failures, heal=heal, journal=journal, log=log)


def show_fields(settings, like=None, client=None):
    """Every field in the tenant, so a mapping can be written from fact rather than guess."""
    entries = (client or Jira(settings['jira'])).fields()
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name, identifier = str(entry.get('name') or ''), str(entry.get('id') or '')
        if like and like.casefold() not in name.casefold() and like.casefold() not in identifier.casefold():
            continue
        schema = entry.get('schema') or {}
        rows.append({'id': identifier, 'name': name, 'custom': bool(entry.get('custom')),
                     'type': schema.get('type') if isinstance(schema, dict) else None})
    return sorted(rows, key=lambda row: (not row['custom'], row['name'].casefold()))


def examine(settings, state_dir=None, log=print):
    """The preflight. Read-only: it asks both systems questions and writes nothing."""
    fireflow, state, _, _ = build(settings, state_dir)
    return diagnose(settings, fireflow, state, log=log)


def show_queue(settings, state_dir=None):
    _, _, failures, _ = build(settings, state_dir)
    return {'pending': failures.pending(), 'parked': failures.parked()}


def release(settings, key, state_dir=None):
    with session(settings, state_dir) as (_, _, failures, journal):
        if failures.release(key):
            journal.write('unparked', key)
            return True
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Poll Jira for access requests and report FireFlow progress back')
    parser.add_argument('--config', required=True, help='bus settings JSON, owner-readable only')
    parser.add_argument('--state-dir', help='override the private state and journal directory')
    modes = parser.add_subparsers(dest='mode', required=True)

    poll = modes.add_parser('poll', help='one pass in each direction')
    writing = poll.add_mutually_exclusive_group()
    writing.add_argument('--apply', action='store_true',
                         help='Actually create requests and comment. Overrides the config.')
    writing.add_argument('--dry-run', action='store_true',
                         help='Change nothing, whatever the config says. Overrides --apply.')
    poll.add_argument('--interval', type=int, default=0,
                      help='Seconds between polls. Zero runs a single pass and exits, which is '
                           'what the systemd timer expects; the loop is for watching by hand.')

    approval = modes.add_parser('capture-approval', help='explicit local administrator approval of an Approved structured issue')
    approval.add_argument('issue_id')
    approval.add_argument('--operator', required=True)

    back_only = modes.add_parser('mirror', help='sync existing FireFlow requests to Jira; never create requests')
    back_only.add_argument('--apply', action='store_true', help='write Jira updates; default is dry run')

    preflight = modes.add_parser('doctor', help='ask both systems whether this config would work')
    preflight.add_argument('--json', action='store_true', help='machine-readable findings')

    listing = modes.add_parser('fields', help='list Jira field names and their ids')
    listing.add_argument('--like', help='only fields whose name or id contains this text')

    audit = modes.add_parser('reconcile', help='check the two systems still agree')
    audit.add_argument('--no-heal', action='store_true',
                       help='Report divergences without repairing even the bus state file.')

    modes.add_parser('queue', help='show failures waiting and failures parked')

    let_go = modes.add_parser('release', help='let a parked failure be tried once more')
    let_go.add_argument('key', help='Jira issue key')

    arguments = parser.parse_args(argv)
    settings = private_json(Path(arguments.config).expanduser(), os.getuid())

    if arguments.mode == 'mirror':
        with session(settings, arguments.state_dir) as (fireflow, state, failures, journal):
            result = mirror(settings, fireflow, state, dry_run=not arguments.apply,
                            failures=failures, journal=journal)
            pending, missing = unresolved(state)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result['failed'] or result['parked'] or pending or missing else 0

    if arguments.mode == 'capture-approval':
        from .approval_gate import ApprovalLedger
        with session(settings, arguments.state_dir) as (_, state, _, journal):
            approval = ApprovalLedger(state.path.parent / 'approvals').capture(
                Jira(settings['jira']), arguments.issue_id, settings['mapping']['structured']['field'],
                settings['fireflow']['template'], settings['fireflow']['devices'], operator=arguments.operator)
            journal.write('approval_captured', None, issue_id=arguments.issue_id,
                          captured_by=approval['captured_by'])
        print('Protected approval captured for issue ' + arguments.issue_id)
        return 0

    if arguments.mode == 'fields':
        rows = show_fields(settings, arguments.like)
        for row in rows:
            print('%-22s %-8s %s' % (row['id'], row['type'] or '', row['name']))
        print('\n%d field(s). Put the id, not the name, into the mapping.' % len(rows))
        return 0

    if arguments.mode == 'doctor':
        report = examine(settings, arguments.state_dir,
                         log=(lambda *a: None) if arguments.json else print)
        if arguments.json:
            print(json.dumps(report['results'], indent=2, ensure_ascii=False))
        # Non-zero on a failure so a person, or a pipeline, can act on it without parsing.
        return 1 if report['failed'] else 0

    if arguments.mode == 'queue':
        print(json.dumps(show_queue(settings, arguments.state_dir), indent=2, ensure_ascii=False))
        return 0

    if arguments.mode == 'release':
        freed = release(settings, arguments.key, arguments.state_dir)
        # An unknown outcome is never released here: the adapter would refuse the repeated
        # operation id anyway, and a person is meant to inspect the ticket first.
        print(json.dumps({'released': freed, 'key': arguments.key}, ensure_ascii=False))
        return 0 if freed else 1

    if arguments.mode == 'reconcile':
        report = check(settings, heal=not arguments.no_heal, state_dir=arguments.state_dir)
        print(json.dumps({'findings': len(report['findings']), 'healed': len(report['healed'])},
                         ensure_ascii=False))
        return 1 if report['findings'] else 0

    writes = apply_mode(settings, arguments)
    while True:
        summary = once(settings, writes, state_dir=arguments.state_dir)
        intake, back = summary['intake'], summary['mirror']
        print(json.dumps({'created': len(intake['created']),
                          'skipped': len(intake['skipped']),
                          'refused': len(intake['refused']),
                          'failed': len(intake.get('failed') or []),
                          'deferred': len(intake['deferred']),
                          'capped': len(intake.get('capped') or []),
                          'mirrored': len(back['updated']),
                          'parked': len(back['parked']),
                          'pending': len(summary['pending']),
                          'missing_ids': len(summary['missing_ids']),
                          'applied': writes}, ensure_ascii=False))
        if arguments.interval <= 0:
            # Non-zero when anything was left undone, so a failed timer unit is enough to
            # notice it. A pass that refused every issue it saw is not a successful pass.
            return 1 if outstanding(summary) else 0
        time.sleep(max(30, arguments.interval))


if __name__ == '__main__':
    raise SystemExit(main())
