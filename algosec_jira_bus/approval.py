"""Content binding for a trusted approval capture, not approval authorization.

The caller must obtain approved_hash from protected approval state, bound to the
same issue and approver, and enforce approval/edit controls. A user-editable hash
alongside the request proves nothing. This helper does not fetch Jira, authorize
approvers, prevent races, or bind deployment configuration (template/devices).
It is intentionally not wired into intake until those controls exist.

The v1 digest covers the normalized structured domain; ignored presentation keys
are not approval content. Row/service order is preserved conservatively. Changing
normalization semantics requires a new digest version and renewed approval.
"""
import hashlib
import hmac
import json
import re

from .structured import normalize


class ApprovalError(ValueError):
    """The supplied trusted approval does not cover the current request."""


def _digest(normalized):
    envelope = {'approvalSchemaVersion': 1, 'requestSchemaVersion': 1,
                'request': normalized}
    canonical = json.dumps(envelope, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False).encode('utf-8')
    return 'v1:sha256:' + hashlib.sha256(canonical).hexdigest()


def request_hash(raw):
    """Validate v1 input and fingerprint its normalized approval content."""
    return _digest(normalize(raw))


def verify_approved(raw, approved_hash):
    """Return detached normalized data only when its trusted digest matches.

    Use this returned snapshot for downstream mapping rather than rereading a
    mutable input. Matching is a content check only, never proof of permission.
    """
    if not isinstance(approved_hash, str) or not re.fullmatch(
            r'v1:sha256:[0-9a-f]{64}', approved_hash):
        raise ApprovalError('A valid trusted approval hash is required')
    normalized = normalize(raw)
    if not hmac.compare_digest(_digest(normalized), approved_hash):
        raise ApprovalError('Request changed since approval; renewed approval is required')
    return normalized
