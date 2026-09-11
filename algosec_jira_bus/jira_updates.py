"""Opt-in Jira comments and status events delivered to linked FireFlow requests.

The first applied pass establishes a baseline without replaying existing history.
Stable operation ids let the FireFlow adapter reconcile a successful remote write
when the process stopped before the local cursor was saved.
"""
from copy import deepcopy
import hashlib
import json

from .adf import cell_text
from .jira import Jira, ISSUE_ID
from .journal import Silent

ORIGIN_PROPERTY = 'algosec-jira-bus.origin'
JIRA_SETTABLE_FIREFLOW_STATUSES = frozenset(
    ('open', 'resolved', 'cancelled', 'rejected'))
MAX_MIRRORED_COMMENT = 4000


def validate_jira_to_fireflow(config):
    """Return normalized settings, or None when this direction is disabled."""
    if config is None or config is False:
        return None
    if not isinstance(config, dict):
        raise ValueError('jira_to_fireflow must be an object')
    enabled = config.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('jira_to_fireflow.enabled must be true or false')
    if not enabled:
        return None
    comments = config.get('comments', False)
    if type(comments) is not bool:
        raise ValueError('jira_to_fireflow.comments must be true or false')
    mapping = config.get('status_map', {})
    if not isinstance(mapping, dict):
        raise ValueError('jira_to_fireflow.status_map must be an object')
    if any(not isinstance(key, str) or not key.strip()
           or not isinstance(value, str) or not value.strip()
           or any(c in key + value for c in '\r\n\x00')
           for key, value in mapping.items()):
        raise ValueError('jira_to_fireflow.status_map requires nonempty status names or ids')
    unsupported = sorted(set(mapping.values()) - JIRA_SETTABLE_FIREFLOW_STATUSES)
    if unsupported:
        raise ValueError('jira_to_fireflow.status_map contains unsafe or unsupported FireFlow '
                         'statuses: %s' % ', '.join(unsupported))
    return {'enabled': True, 'comments': comments, 'status_map': dict(mapping)}


def _origin(event):
    properties = event.get('properties') or []
    if isinstance(properties, list) and any(
            isinstance(p, dict) and p.get('key') == ORIGIN_PROPERTY
            and p.get('value') in ('fireflow', {'source': 'fireflow'}) for p in properties):
        return True
    metadata = event.get('historyMetadata') or {}
    extra = metadata.get('extraData') if isinstance(metadata, dict) else None
    return isinstance(extra, dict) and extra.get(ORIGIN_PROPERTY) in (
        'fireflow', {'source': 'fireflow'})


def _restricted_comment(event):
    if event.get('visibility'):
        return True
    for prop in event.get('properties') or []:
        if not isinstance(prop, dict) or prop.get('key') != 'sd.public.comment':
            continue
        value = prop.get('value')
        if value is False or (isinstance(value, dict) and value.get('internal') is True):
            return True
    return False


def _events(rows):
    if not isinstance(rows, list):
        raise ValueError('Jira events must be a complete list')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not ISSUE_ID.fullmatch(str(row.get('id', ''))):
            raise ValueError('Jira event requires an immutable numeric id')
        identifier = str(row['id'])
        if identifier in result and result[identifier] != row:
            raise ValueError('Conflicting duplicate Jira event')
        result[identifier] = row
    return sorted(result.values(), key=lambda row: int(row['id']))


def _operation_id(issue_id, kind, event_id):
    identity = json.dumps([issue_id, kind, event_id], separators=(',', ':'))
    return 'jira-update-' + hashlib.sha256(identity.encode()).hexdigest()


def _comment(key, row):
    author = row.get('author') or {}
    name = author.get('displayName') or author.get('accountId') or 'Unknown Jira user'
    text = row.get('text')
    if not isinstance(text, str):
        body = row.get('body')
        text = body if isinstance(body, str) else cell_text(body)
    prefix = 'Jira %s | %s | %s\n' % (key, name, row.get('created', ''))
    suffix = '\n[truncated by jira-fireflow-bus]'
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    # FireFlow's RT form rejects C0 controls. Replace them before the durable
    # pending intent is saved so one malformed Jira comment cannot stall an issue.
    body = ''.join(char if ord(char) >= 32 or char in '\n\t' else '\ufffd'
                   for char in normalized).strip()
    if len(prefix) + len(body) > MAX_MIRRORED_COMMENT:
        body = body[:MAX_MIRRORED_COMMENT - len(prefix) - len(suffix)] + suffix
    return prefix + body


def _watermark(identifiers):
    """Keep the one durable high-water id instead of an ever-growing seen list."""
    values = [str(value) for value in identifiers]
    return [max(values, key=int)] if values else []


def _deliver(fireflow, ticket, pending):
    method = fireflow.add_comment if pending['action'] == 'comment' else fireflow.set_status
    return method(ticket, pending['value'], pending['operation_id'], pending['reason'])


def _suppress_mirror(cursor, pending):
    """Persist one Jira-origin status so mirror will not transition it back."""
    if pending.get('action') == 'status':
        cursor['mirror_suppression'] = {
            'event_id': pending['event_id'],
            'jira_status': pending.get('jira_status'),
            'fireflow_target': pending['value'],
            'operation_id': pending['operation_id'],
        }


def sync_updates(settings, fireflow, state, jira=None, dry_run=True, log=print,
                 journal=None):
    """Read complete Jira event streams and checkpoint each confirmed delivery.

    ``add_comment`` must create an internal FireFlow comment. Both adapter mutation
    methods must deduplicate their operation id before retrying an ambiguous write.
    This function never advances a cursor after a failed call or during dry run.
    """
    config = validate_jira_to_fireflow(settings.get('jira_to_fireflow'))
    result = {'bootstrapped': [], 'updated': [], 'unchanged': [], 'failed': [], 'parked': []}
    if config is None or not (config['comments'] or config['status_map']):
        return result
    jira = jira or Jira(settings['jira'])
    journal = journal or Silent()
    for key, entry in sorted(state.entries().items()):
        ticket = entry.get('change_request_id')
        if not ticket:
            continue
        try:
            issue_id = str(entry.get('jira_issue_id') or '')
            if not ISSUE_ID.fullmatch(issue_id):
                raise ValueError('Linked issue lacks its immutable Jira id')
            cursor = deepcopy(entry.get('jira_sync') or {})
            if not isinstance(cursor, dict) or cursor.get('issue_id', issue_id) != issue_id:
                raise ValueError('Invalid Jira synchronization cursor identity')
            cursor['issue_id'] = issue_id
            if any(isinstance(cursor.get(kind), dict)
                   and (cursor[kind].get('pending') or {}).get('parked')
                   for kind in ('comments', 'statuses')):
                result['parked'].append(key)
                continue
            # Read both streams before persisting a bootstrap. Partial pagination or
            # a failed read must never turn unseen history into a baseline.
            streams = []
            if config['comments']:
                comment_cursor = cursor.get('comments')
                comment_after = (max(comment_cursor.get('seen', ()), key=int)
                                 if isinstance(comment_cursor, dict)
                                 and comment_cursor.get('seen') else None)
                streams.append(('comments', _events(jira.comments(
                    issue_id, after_id=comment_after))))
            if config['status_map']:
                status_cursor = cursor.get('statuses')
                status_after = (max(status_cursor.get('seen', ()), key=int)
                                if isinstance(status_cursor, dict)
                                and status_cursor.get('seen') else None)
                streams.append(('statuses', _events(jira.changelog(
                    issue_id, after_id=status_after))))
            changed, bootstrapped = False, False
            for kind, rows in streams:
                if kind not in cursor:
                    cursor[kind] = {'seen': _watermark(row['id'] for row in rows)}
                    if not dry_run:
                        state.record(key, {'jira_sync': deepcopy(cursor)})
                    bootstrapped = True
                    continue
                stream = cursor[kind]
                if (not isinstance(stream, dict) or not isinstance(stream.get('seen'), list)
                        or any(not isinstance(i, str) or not ISSUE_ID.fullmatch(i)
                               for i in stream['seen'])):
                    raise ValueError('Invalid Jira synchronization event cursor')
                seen = set(stream['seen'])
                pending = stream.get('pending')
                if pending:
                    changed = True
                    if not dry_run:
                        _deliver(fireflow, ticket, pending)
                        _suppress_mirror(cursor, pending)
                        seen.add(pending['event_id'])
                        stream['seen'] = _watermark(seen)
                        stream.pop('pending')
                        state.record(key, {'jira_sync': deepcopy(cursor)})
                    else:
                        # Preview the pending write without also planning it from rows.
                        seen.add(pending['event_id'])
                for row in rows:
                    event_id = str(row['id'])
                    if seen and int(event_id) <= max(map(int, seen)):
                        continue
                    operation = _operation_id(issue_id, kind, event_id)
                    reason = 'Synchronize Jira %s %s event %s' % (key, kind, event_id)
                    action = None
                    if not _origin(row) and not (kind == 'comments'
                                                  and _restricted_comment(row)):
                        if kind == 'comments':
                            action = ('comment', _comment(key, row))
                        else:
                            if row.get('kind') == 'other':
                                action = None
                            elif not isinstance(row.get('to'), str) or not row['to']:
                                raise ValueError('Jira status event is missing its target')
                            else:
                                target = config['status_map'].get(row.get('to_id'))
                                if target is None:
                                    target = config['status_map'].get(row['to'])
                                if target is not None:
                                    action = ('status', target)
                    if action:
                        changed = True
                        log('%s Jira %s event %s for %s' % (
                            'would deliver' if dry_run else 'delivering', kind, event_id, key))
                        if not dry_run:
                            stream['pending'] = {'action': action[0], 'value': action[1],
                                                 'operation_id': operation, 'reason': reason,
                                                 'event_id': event_id,
                                                 **({'jira_status': row.get('to')}
                                                    if action[0] == 'status' else {})}
                            state.record(key, {'jira_sync': deepcopy(cursor)})
                            _deliver(fireflow, ticket, stream['pending'])
                            _suppress_mirror(cursor, stream['pending'])
                            stream.pop('pending')
                    if not dry_run:
                        seen.add(event_id)
                        stream['seen'] = _watermark(seen)
                        state.record(key, {'jira_sync': deepcopy(cursor)})
                        if action:
                            journal.write('jira_to_fireflow', key, event=kind,
                                          event_id=event_id, operation_id=operation)
            if bootstrapped:
                result['bootstrapped'].append(key)
            if changed:
                result['updated'].append(key)
            elif not bootstrapped:
                result['unchanged'].append(key)
        except Exception as error:
            unknown = (getattr(error, 'outcome_unknown', False)
                       or type(error).__name__ == 'FireFlowMutationUnknown')
            if unknown and not dry_run:
                for kind in ('comments', 'statuses'):
                    stream = cursor.get(kind) or {}
                    if stream.get('pending'):
                        stream['pending']['parked'] = True
                state.record(key, {'jira_sync': deepcopy(cursor)})
                result['parked'].append(key)
            else:
                result['failed'].append(key)
            log('could not synchronize Jira updates for %s: %s' % (key, type(error).__name__))
            if not dry_run:
                journal.write('error', key, stage='jira_to_fireflow', error=type(error).__name__)
    return result
