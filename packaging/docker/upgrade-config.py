#!/usr/bin/env python3
"""Stage and atomically apply non-secret Docker configuration migrations."""
import argparse
import json
import os
from pathlib import Path
import stat
import tempfile


MAX_BYTES = 1024 * 1024


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def read_private(path, expected_uid):
    path = Path(path)
    flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
             getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError('Configuration must be an owner-only regular file') from None
    try:
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != expected_uid or
                details.st_nlink != 1 or details.st_mode & 0o077 or
                details.st_size > MAX_BYTES):
            raise ValueError('Configuration must be an owner-only regular file')
        raw = os.read(descriptor, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or os.read(descriptor, 1):
            raise ValueError('Configuration is too large')
    finally:
        os.close(descriptor)
    try:
        return raw, json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                               parse_constant=lambda _value: (_ for _ in ()).throw(
                                   ValueError('non-JSON number')))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError('Configuration must contain strict UTF-8 JSON') from None


def migrate(settings):
    if not isinstance(settings, dict):
        raise ValueError('Configuration root must be an object')
    fireflow = settings.get('fireflow')
    if not isinstance(fireflow, dict):
        raise ValueError('Configuration must contain fireflow settings')
    fields = fireflow.get('allowed_fields')
    if (not isinstance(fields, list) or
            any(not isinstance(field, str) or not field for field in fields)):
        raise ValueError('fireflow.allowed_fields must be a list of names')
    if 'requestor' not in {field.casefold() for field in fields}:
        fields.append('Requestor')
    reverse = settings.setdefault('jira_to_fireflow', {
        'enabled': False,
        'comments': True,
        'status_map': {},
    })
    if not isinstance(reverse, dict):
        raise ValueError('jira_to_fireflow must be an object')
    fireflow.setdefault('legacy_rt_enabled', False)
    return settings


def atomic_write(path, content, uid, gid):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent,
                                              prefix='.' + path.name + '.')
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            os.fchown(output.fileno(), uid, gid)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def stage(source, output, expected_uid):
    _, settings = read_private(source, expected_uid)
    migrated = migrate(settings)
    encoded = (json.dumps(migrated, indent=2, ensure_ascii=False) + '\n').encode()
    atomic_write(output, encoded, expected_uid, Path(source).stat().st_gid)


def apply(source, staged, backup, expected_uid):
    source = Path(source)
    backup = Path(backup)
    if backup.exists() or backup.is_symlink():
        raise ValueError('A previous upgrade backup already exists')
    original, _ = read_private(source, expected_uid)
    migrated, _ = read_private(staged, expected_uid)
    details = source.stat()
    atomic_write(backup, original, expected_uid, details.st_gid)
    atomic_write(source, migrated, expected_uid, details.st_gid)


def restore(source, backup, expected_uid):
    content, _ = read_private(backup, expected_uid)
    details = Path(source).stat()
    atomic_write(source, content, expected_uid, details.st_gid)
    Path(backup).unlink()


def discard(backup, expected_uid):
    read_private(backup, expected_uid)
    Path(backup).unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stage', 'apply', 'restore', 'discard'))
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--expected-uid', type=int, default=10001)
    args = parser.parse_args()
    if args.action == 'stage' and len(args.paths) == 2:
        stage(*args.paths, args.expected_uid)
    elif args.action == 'apply' and len(args.paths) == 3:
        apply(*args.paths, args.expected_uid)
    elif args.action == 'restore' and len(args.paths) == 2:
        restore(*args.paths, args.expected_uid)
    elif args.action == 'discard' and len(args.paths) == 1:
        discard(*args.paths, args.expected_uid)
    else:
        parser.error('wrong number of paths for action')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
