"""Protected local approval capture for an explicitly administered pilot.

Capture is a trusted local administrator action, never an automatic consequence
of polling Approved issues. Jira status alone does not authenticate an approver.
Both operations reread Jira; verification returns that exact fresh snapshot for
mapping. It cannot lock Jira against an edit between this read and FireFlow POST.
Production must enforce approval/edit permissions or an immutable submission.
"""
import copy
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from datetime import datetime, timezone

from .approval import ApprovalError, request_hash


def issue_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value):
        raise ApprovalError('An immutable numeric Jira issue ID is required')
    return value


def _protected(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ApprovalError('Approval storage must be owner-only and owned by this user')


class ApprovalLedger:
    """One atomically replaced approval per Jira issue ID in a private directory.

    Use one directory per integration. Administrators who can edit this directory
    can approve requests; it must not be writable by Jira requesters.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _protected(self.directory.lstat(), directory=True)

    def _fresh(self, jira, identifier, field, template, devices):
        _protected(self.directory.lstat(), directory=True)
        identifier = issue_id(identifier)
        if not isinstance(field, str) or not re.fullmatch(r'customfield_[0-9]+', field):
            raise ApprovalError('Structured request must use a Jira custom field')
        if not isinstance(template, str) or not template.strip() or not isinstance(devices, list) \
                or not devices or any(not isinstance(d, str) or not d.strip() for d in devices):
            raise ApprovalError('Approval requires an explicit template and device list')
        origin = jira.config.get('base_url')
        if not isinstance(origin, str) or not origin:
            raise ApprovalError('Jira origin is required to bind approval')
        issue = jira.read_issue(identifier, {field, 'status', 'summary'})
        if not isinstance(issue, dict) or issue.get('id') != identifier:
            raise ApprovalError('Jira returned a different immutable issue ID')
        fields = issue.get('fields') or {}
        status = fields.get('status') or {}
        if not isinstance(status, dict) or status.get('name') != 'Approved':
            raise ApprovalError('Fresh Jira status must be Approved')
        binding = {'schema_version': 1, 'issue_id': identifier,
                   'jira_origin': origin.rstrip('/'), 'field': field,
                   'request_hash': request_hash(fields.get(field)),
                   'template': template, 'devices': list(devices)}
        return copy.deepcopy(issue), binding

    def capture(self, jira, identifier, field, template, devices, *, operator):
        """Explicitly attest the currently Approved payload as a local pilot admin."""
        if not isinstance(operator, str) or not operator.strip():
            raise ApprovalError('Record the administrator performing approval capture')
        _, binding = self._fresh(jira, identifier, field, template, devices)
        record = {'binding': binding, 'captured_by': operator.strip(),
                  'captured_at': datetime.now(timezone.utc).isoformat()}
        destination = self.directory / (issue_id(identifier) + '.json')
        fd, temporary = tempfile.mkstemp(prefix='.approval-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return record

    def verify(self, jira, identifier, field, template, devices):
        """Return the fresh issue only if status, content and deployment still match."""
        fresh, binding = self._fresh(jira, identifier, field, template, devices)
        try:
            fd = os.open(self.directory / (issue_id(identifier) + '.json'),
                         os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                _protected(os.fstat(stream.fileno()))
                record = json.load(stream)
        except (OSError, ValueError) as error:
            raise ApprovalError('A valid protected approval capture is required') from error
        saved = record.get('binding') if isinstance(record, dict) else None
        expected = json.dumps(binding, sort_keys=True, separators=(',', ':'))
        actual = json.dumps(saved, sort_keys=True, separators=(',', ':'))
        if not hmac.compare_digest(expected.encode('utf-8'), actual.encode('utf-8')):
            raise ApprovalError('Request or deployment changed; renewed approval is required')
        return fresh
