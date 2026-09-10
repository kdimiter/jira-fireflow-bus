"""Failures that must not be lost, and the rule about which ones may be tried again.

A poll that hits a network error must not silently drop the event: the next pass would
skip the issue as already seen, or retry a change nobody is allowed to retry. So every
per-issue failure lands in a queue with an attempt count and a time it is next due.

**Which failures may be tried again is not a judgement call.** The bus contract is
that a mutation whose outcome is unknown is never repeated automatically, and its FireFlow
adapter enforces this itself: it writes a ``.started.json`` receipt *before* it sends
anything, and refuses a second call with the same operation id. That receipt is the signal
this module reads -- far more reliable than matching on the text of an error:

* **receipt present** -- the request may have reached the appliance. Never retried. Parked
  for a person to inspect the ticket and the receipt, which is exactly what the adapter's
  own error message asks for.
* **no receipt** -- the adapter refused during validation and nothing was sent (an
  allowlist or template that does not match the deployment). Safe to try again slowly,
  because that is the kind of thing an operator fixes between two polls.
* **reads and Jira calls** -- retried with backoff. Nothing in FireFlow was touched.

Attempts are bounded either way. A queue that retries forever is a queue nobody looks at.
"""
import time

# The first retry waits a minute, then doubles, and never sleeps longer than an hour: a
# backoff measured in days is indistinguishable from a bus that has stopped working. Eight
# attempts spans a little over four hours, long enough to ride out a maintenance window and
# short enough that a genuinely broken deployment surfaces the same working day.
BASE = 60
FACTOR = 2
CAP = 3600
ATTEMPTS = 8

# A refused create is retried on the slow schedule only: nothing was sent, so repeating it
# is safe, but the cause is a configuration mismatch and hammering it helps nobody.
SLOW = CAP


def delay(attempts, slow=False):
    """Seconds to wait before attempt number ``attempts`` + 1."""
    if slow:
        return SLOW
    return min(CAP, BASE * (FACTOR ** max(0, attempts - 1)))


def started_receipt(fireflow, operation_id):
    """Path of the adapter's pre-execution receipt for this operation, or None.

    Mirrors ``FireFlow._mutate``: the receipt name is a digest of the server address and
    the operation id, so it identifies the attempt independently of this process.
    """
    try:
        from .fireflow import digest
        key = digest([fireflow.config['base_url'], operation_id])
        return fireflow.state / (key + '.started.json')
    except Exception:
        return None


def receipt_pairs(fireflow, operation_id):
    """Receipt pairs for the canonical and any preserved pre-upgrade origin."""
    try:
        from .fireflow import digest
        origins = getattr(fireflow, 'receipt_origins',
                          (fireflow.config['base_url'],))
        keys = tuple(dict.fromkeys(digest([origin, operation_id]) for origin in origins))
        return tuple((fireflow.state / (key + '.started.json'),
                      fireflow.state / (key + '.result.json')) for key in keys)
    except Exception:
        return ()


def receipt_candidates(fireflow, operation_id):
    """All files that can prove an attempt began or completed."""
    return tuple(path for pair in receipt_pairs(fireflow, operation_id) for path in pair)


def sent_anything(fireflow, operation_id):
    """True when the adapter had already begun this mutation, so it must not be repeated."""
    paths = receipt_candidates(fireflow, operation_id)
    # No receipt directory to consult means no proof that nothing was sent. Treat the
    # outcome as unknown, which is the side that never retries.
    if not paths:
        return True
    try:
        from .fireflow import receipt_exists
        return any(receipt_exists(path) for path in paths)
    except OSError:
        return True


class Failures:
    """The queue itself, stored alongside the issue state so one write covers both.

    Keeping failures in the same file as the issues is deliberate: a crash between two
    files could leave an issue recorded as created with its failure still pending, or the
    reverse. One atomic replace of one file cannot tear.
    """

    def __init__(self, state, now=time.time):
        self.state = state
        self.now = now
        self.store = state.data.setdefault('failures', {})

    def record(self, key, stage, error, retryable=True, slow=False):
        """Note a failure and say when, if ever, it may be attempted again."""
        entry = dict(self.store.get(key) or {})
        attempts = int(entry.get('attempts') or 0) + 1
        moment = self.now()
        entry.update({'stage': stage,
                      'error': type(error).__name__ if isinstance(error, BaseException) else str(error)[:200],
                      'message': str(error)[:500],
                      'attempts': attempts,
                      'first_seen': entry.get('first_seen') or moment,
                      'last_seen': moment,
                      'slow': bool(slow)})
        if not retryable:
            entry.update({'parked': True, 'reason': 'outcome_unknown', 'next_attempt': None})
        elif attempts >= ATTEMPTS:
            entry.update({'parked': True, 'reason': 'attempts_exhausted', 'next_attempt': None})
        else:
            entry.update({'parked': False, 'reason': None,
                          'next_attempt': moment + delay(attempts, slow)})
        self.store[key] = entry
        self.state.save()
        return entry

    def clear(self, key):
        if self.store.pop(key, None) is not None:
            self.state.save()
            return True
        return False

    def entry(self, key):
        return self.store.get(key)

    def blocked(self, key):
        """True when this issue must be left alone on this pass."""
        entry = self.store.get(key)
        if not entry:
            return False
        if entry.get('parked'):
            return True
        due = entry.get('next_attempt')
        return bool(due) and self.now() < due

    def parked(self):
        return {key: entry for key, entry in self.store.items() if entry.get('parked')}

    def pending(self):
        return {key: entry for key, entry in self.store.items() if not entry.get('parked')}

    def release(self, key):
        """Let a parked entry be tried once more. Only ever called for a person's decision.

        An ``outcome_unknown`` entry is never released here: the adapter would refuse the
        repeated operation id anyway, and the point of parking it is that a person has to
        look at the ticket first.
        """
        entry = self.store.get(key)
        if not entry or not entry.get('parked') or entry.get('reason') == 'outcome_unknown':
            return False
        entry.update({'parked': False, 'reason': None, 'attempts': 0, 'next_attempt': None})
        self.state.save()
        return True
