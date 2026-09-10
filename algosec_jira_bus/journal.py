"""Append-only record of what the bus did, so a pass can be reconstructed afterwards.

The FireFlow adapter already writes receipts for the changes it submits, and those prove
what reached the appliance. They say nothing about the issues the bus *refused*, the ones
it skipped, the statuses it mirrored or the failures it parked -- and those are exactly
the questions asked when an access request did not appear where somebody expected it.

Rotation, locking, the 0600 mode and the private state directory come from the bus-owned
``Audit`` implementation. The journal points that machinery at a different file so there
is one implementation of the careful part rather than two that drift.
"""
import json
import os
import time

from .runtime import Audit, redact

from .jira import KEY

# Closed set: an unexpected value is recorded
# as 'invalid' instead of echoing text of unknown origin into the journal.
KINDS = frozenset((
    'pass', 'created', 'skipped', 'refused', 'mirrored', 'transition',
    'error', 'retry', 'parked', 'reconcile', 'unparked', 'approval_captured',
))


class Journal(Audit):
    """``Audit`` writing bus events to ``jira-bus.jsonl`` in the same state directory."""

    def __init__(self, directory):
        super().__init__(directory)
        self.path = self.directory / 'jira-bus.jsonl'

    def write(self, kind, key=None, **detail):
        record = {'time': time.time(),
                  'kind': kind if kind in KINDS else 'invalid',
                  'key': key if key and KEY.fullmatch(key) else None,
                  'pid': os.getpid(),
                  'detail': detail or None}
        line = json.dumps(redact(record), sort_keys=True, ensure_ascii=False)
        self._append((line + '\n').encode())
        return record


class Silent:
    """Journal-shaped no-op, for callers that have not been given a state directory."""

    def write(self, kind, key=None, **detail):
        return None
