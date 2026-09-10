import json
import os
from pathlib import Path


def resolve_secret(reference):
    if not isinstance(reference, str) or not reference.startswith('env:'):
        raise ValueError('Test connector supports env: references only')
    value = os.environ.get(reference[4:])
    if not value:
        raise ValueError('Secret is unavailable')
    return value


def private_json(path, uid):
    path = Path(path)
    info = path.stat()
    if info.st_uid != uid or info.st_mode & 0o077:
        raise ValueError('Configuration must be owner-readable only')
    return json.loads(path.read_text())
