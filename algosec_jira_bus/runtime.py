"""Private state directories, redaction and bounded append-only audit files."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat


REDACTED = '[REDACTED]'
MAX_AUDIT_BYTES = 8 * 1024 * 1024
MAX_AUDIT_BACKUPS = 10
MAX_REDACT_DEPTH = 32
MAX_REDACT_ITEMS = 1000
SENSITIVE_KEY = re.compile(
    r'(?i)(?:password|passwd|token|secret|authorization|cookie|api[_-]?key|session|email)')
INLINE_SECRET = re.compile(
    r'(?i)\b(password|passwd|token|secret|api[_-]?key|authorization|cookie)'
    r'(\s*[:=]\s*)(?:"[^"]*"|\'[^\']*\'|[^\s,;]+)')
BEARER_SECRET = re.compile(r'(?i)\b(Bearer)\s+[^\s,;]+')
SAFE_OPERATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
SAFE_KIND = re.compile(r'[a-z][a-z0-9_]{0,63}')


def secure_dir(path):
    """Create or validate a directory accessible only to this process owner."""
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        information = path.lstat()
    except OSError:
        raise ValueError('Private directory required') from None
    if (not stat.S_ISDIR(information.st_mode) or stat.S_ISLNK(information.st_mode) or
            information.st_uid != os.getuid() or information.st_mode & 0o077):
        raise ValueError('Private directory required')
    return path


def redact(value):
    """Return JSON-shaped data with common credential forms removed."""
    return _redact(value, 0, set())


def _redact(value, depth, active):
    if depth > MAX_REDACT_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            return REDACTED
        active.add(identity)
        try:
            output = {}
            for number, (key, item) in enumerate(value.items()):
                if number >= MAX_REDACT_ITEMS:
                    output['[TRUNCATED]'] = True
                    break
                text_key = str(key)
                output[text_key] = (REDACTED if SENSITIVE_KEY.search(text_key) else
                                    _redact(item, depth + 1, active))
            return output
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            return [REDACTED]
        active.add(identity)
        try:
            result = [_redact(item, depth + 1, active)
                      for item in value[:MAX_REDACT_ITEMS]]
            if len(value) > MAX_REDACT_ITEMS:
                result.append('[TRUNCATED]')
            return result
        finally:
            active.remove(identity)
    if isinstance(value, str):
        value = BEARER_SECRET.sub(lambda match: match.group(1) + ' ' + REDACTED, value)
        value = INLINE_SECRET.sub(lambda match: match.group(1) + match.group(2) + REDACTED,
                                  value)
        return value
    return value


def _validate_private_file(descriptor):
    information = os.fstat(descriptor)
    if (not stat.S_ISREG(information.st_mode) or information.st_uid != os.getuid() or
            information.st_nlink != 1 or information.st_mode & 0o077):
        raise ValueError('Audit path must be a private regular file')
    return information


class Audit:
    """Process-safe JSONL audit with owner-only files and bounded rotation."""

    def __init__(self, directory, *, max_bytes=MAX_AUDIT_BYTES,
                 max_backups=MAX_AUDIT_BACKUPS):
        if (type(max_bytes) is not int or max_bytes < 128 or
                type(max_backups) is not int or not 0 <= max_backups <= 100):
            raise ValueError('Invalid audit retention limits')
        self.directory = secure_dir(directory)
        self.path = self.directory / 'audit.jsonl'
        self.max_bytes = max_bytes
        self.max_backups = max_backups
        self._lock_path = self.directory / '.audit.lock'

    def _open_private(self, path, flags):
        try:
            descriptor = os.open(path, flags | getattr(os, 'O_CLOEXEC', 0) |
                                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except OSError:
            raise ValueError('Audit path must be a private regular file') from None
        try:
            information = _validate_private_file(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, information

    @contextmanager
    def _locked(self):
        descriptor, _ = self._open_private(
            self._lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _existing_private(self, path):
        try:
            descriptor, information = self._open_private(
                path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        except ValueError:
            if not path.exists() and not path.is_symlink():
                return None
            raise
        os.close(descriptor)
        return information

    def _rotate_unlocked(self):
        current = self._existing_private(self.path)
        if current is None:
            return
        if self.max_backups == 0:
            self.path.unlink()
        else:
            oldest = self.path.with_name(self.path.name + '.%d' % self.max_backups)
            if self._existing_private(oldest) is not None:
                oldest.unlink()
            for number in range(self.max_backups - 1, 0, -1):
                source = self.path.with_name(self.path.name + '.%d' % number)
                if self._existing_private(source) is not None:
                    source.replace(self.path.with_name(self.path.name + '.%d' % (number + 1)))
            self.path.replace(self.path.with_name(self.path.name + '.1'))
        directory = os.open(self.directory, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _rotate(self):
        with self._locked():
            self._rotate_unlocked()

    def _append(self, data):
        if not isinstance(data, bytes):
            raise TypeError('Audit data must be bytes')
        if len(data) > self.max_bytes:
            raise ValueError('Audit record is too large')
        with self._locked():
            descriptor, information = self._open_private(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            if information.st_size + len(data) > self.max_bytes:
                os.close(descriptor)
                self._rotate_unlocked()
                descriptor, _ = self._open_private(
                    self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written < 1:
                        raise OSError('Audit append made no progress')
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def record(self, operation_id, kind, detail=None):
        record = {
            'operation_id': operation_id if isinstance(operation_id, str) and
            SAFE_OPERATION.fullmatch(operation_id) else None,
            'kind': kind if isinstance(kind, str) and SAFE_KIND.fullmatch(kind) else 'invalid',
            'detail': detail,
        }
        record = redact(record)
        try:
            line = json.dumps(record, sort_keys=True, ensure_ascii=False,
                              separators=(',', ':')).encode('utf-8') + b'\n'
        except (TypeError, ValueError):
            raise ValueError('Audit detail must be JSON serializable') from None
        self._append(line)
        return record
