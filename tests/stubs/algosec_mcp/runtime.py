import json
import os
from pathlib import Path
import re


def secure_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not path.is_dir() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Private directory required')
    return path


def redact(value):
    if isinstance(value, dict):
        return {
            key: ('[REDACTED]' if any(word in key.lower() for word in
                                      ('password', 'token', 'secret', 'authorization', 'cookie'))
                  else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'(?i)(password|token|secret)=\S+', r'\1=[REDACTED]', value)
    return value


class Audit:
    def __init__(self, directory):
        self.directory = secure_dir(directory)
        self.path = self.directory / 'audit.jsonl'

    def _rotate(self):
        return None

    def _append(self, data):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(data)

    def record(self, operation_id, kind, detail=None):
        record = redact({'operation_id': operation_id, 'kind': kind, 'detail': detail})
        self._append((json.dumps(record, sort_keys=True) + '\n').encode())
        return record
