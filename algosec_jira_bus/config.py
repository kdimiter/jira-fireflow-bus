"""Small, strict configuration and secret readers owned by the bus."""

import json
import os
from pathlib import Path
import re
import stat


MAX_CONFIG_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 64 * 1024
ENVIRONMENT_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}')


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Configuration contains duplicate key %r' % key)
        result[key] = value
    return result


def _reject_non_json_number(value):
    raise ValueError('Configuration contains a non-JSON number')


def private_json(path, expected_uid=None, *, max_bytes=MAX_CONFIG_BYTES):
    """Read one owner-only regular JSON file without following its final path.

    The descriptor is validated after opening, closing the usual check-then-open race.
    A single hard link is required so another path cannot be used to replace the contents
    behind an otherwise private-looking configuration file.
    """
    path = Path(path)
    uid = os.getuid() if expected_uid is None else expected_uid
    if type(uid) is not int or type(max_bytes) is not int or max_bytes < 1:
        raise ValueError('Invalid private JSON reader limits')
    flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
             getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError:
        raise ValueError('Configuration must be a regular owner-only file') from None
    try:
        information = os.fstat(descriptor)
        if (not stat.S_ISREG(information.st_mode) or information.st_uid != uid or
                information.st_nlink != 1 or information.st_mode & 0o077):
            raise ValueError('Configuration must be a regular owner-only file')
        if information.st_size > max_bytes:
            raise ValueError('Configuration is too large')
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) > max_bytes:
            raise ValueError('Configuration is too large')
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode('utf-8'),
                           object_pairs_hook=_object_without_duplicate_keys,
                           parse_constant=_reject_non_json_number)
    except UnicodeDecodeError:
        raise ValueError('Configuration must contain valid UTF-8 JSON') from None
    except (json.JSONDecodeError, RecursionError):
        raise ValueError('Configuration must contain valid JSON') from None
    if not isinstance(value, dict):
        raise ValueError('Configuration root must be a JSON object')
    return value


def resolve_secret(reference):
    """Resolve an explicit environment reference without invoking a shell."""
    if not isinstance(reference, str) or not reference.startswith('env:'):
        raise ValueError('Secret reference must use env:NAME')
    name = reference[4:]
    if not ENVIRONMENT_NAME.fullmatch(name):
        raise ValueError('Secret reference must use a valid environment variable name')
    value = os.environ.get(name)
    if not value:
        raise ValueError('Secret is unavailable')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Secret must be valid UTF-8 text') from None
    if len(encoded) > MAX_SECRET_BYTES:
        raise ValueError('Secret is too large')
    return value
