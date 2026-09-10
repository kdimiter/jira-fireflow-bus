#!/usr/bin/env python3
"""Validate and atomically stage a non-interactive container configuration."""

import argparse
import json
import os
from pathlib import Path
import ssl
import stat
import tempfile


CONFIG_LIMIT = 1024 * 1024
SECRET_LIMIT = 64 * 1024
CA_LIMIT = 1024 * 1024
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError('non-JSON number')


def read_private(path, limit, expected_uid=0):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('input paths must be absolute')
    flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
             getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError('input must be a readable private regular file') from None
    try:
        information = os.fstat(descriptor)
        if (not stat.S_ISREG(information.st_mode) or information.st_uid != expected_uid or
                information.st_nlink != 1 or stat.S_IMODE(information.st_mode) != 0o600 or
                information.st_size > limit):
            raise ValueError('input must be an owner-only regular file')
        chunks = []
        remaining = limit + 1
        while remaining:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b''.join(chunks)
        if len(payload) > limit:
            raise ValueError('input file is too large')
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                information.st_size, information.st_mtime_ns, information.st_ctime_ns):
            raise ValueError('input changed while it was being read')
        return payload
    finally:
        os.close(descriptor)


def read_json(payload, label):
    try:
        value = json.loads(payload.decode('utf-8'), object_pairs_hook=unique_object,
                           parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError(label + ' must contain strict UTF-8 JSON') from None
    if not isinstance(value, dict):
        raise ValueError(label + ' root must be a JSON object')
    return value


def validate(config_path, secrets_path, ca_path=None, expected_uid=0):
    config = read_json(read_private(config_path, CONFIG_LIMIT, expected_uid), 'config')
    secrets = read_json(read_private(secrets_path, SECRET_LIMIT, expected_uid), 'secrets')
    jira, fireflow = config.get('jira'), config.get('fireflow')
    if not isinstance(jira, dict) or not isinstance(fireflow, dict):
        raise ValueError('config must contain jira and fireflow objects')
    if jira.get('token_ref') != 'env:JIRA_API_TOKEN':
        raise ValueError('jira.token_ref must be env:JIRA_API_TOKEN')
    if fireflow.get('password_ref') != 'env:ASMS_API_PASSWORD':
        raise ValueError('fireflow.password_ref must be env:ASMS_API_PASSWORD')
    if 'session_ref' in fireflow:
        raise ValueError('fireflow.session_ref is unsupported in the container installer')
    if set(secrets) != {'JIRA_API_TOKEN', 'ASMS_API_PASSWORD'}:
        raise ValueError('secrets must contain JIRA_API_TOKEN and ASMS_API_PASSWORD only')
    if any(not isinstance(value, str) or not value or any(c in value for c in '\r\n\0')
           for value in secrets.values()):
        raise ValueError('secret values must be nonempty single-line strings')
    ca_payload = None
    if ca_path:
        ca_payload = read_private(ca_path, CA_LIMIT, expected_uid)
        try:
            ssl.create_default_context(cadata=ca_payload.decode('ascii'))
        except (UnicodeDecodeError, ssl.SSLError):
            raise ValueError('CA file must contain a valid PEM certificate bundle') from None
        fireflow['ca_file'] = '/etc/algosec-jira-bus/ca.pem'
    elif fireflow.get('ca_file'):
        raise ValueError('config sets ca_file; provide its PEM bundle with --ca-file')
    return ((json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
            (json.dumps(secrets, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'),
            ca_payload)


def install(target, files, target_uid=10001, target_gid=10001):
    target = Path(target)
    information = target.lstat()
    if (not stat.S_ISDIR(information.st_mode) or stat.S_ISLNK(information.st_mode) or
            information.st_uid != target_uid or information.st_gid != target_gid or
            stat.S_IMODE(information.st_mode) != 0o700):
        raise ValueError('target config directory must be private and owned by UID/GID 10001')
    payloads = {'bus.json': files[0], 'secrets.json': files[1]}
    if files[2] is not None:
        payloads['ca.pem'] = files[2]
    staged = []
    try:
        for name, payload in payloads.items():
            descriptor, temporary = tempfile.mkstemp(prefix='.' + name + '.', dir=target)
            staged.append(Path(temporary))
            try:
                os.fchmod(descriptor, 0o600)
                os.fchown(descriptor, target_uid, target_gid)
                with os.fdopen(descriptor, 'wb') as stream:
                    descriptor = -1
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        for temporary, name in zip(staged, payloads):
            os.replace(temporary, target / name)
        directory = os.open(target, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        for temporary in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--secrets', required=True)
    parser.add_argument('--ca')
    parser.add_argument('--target')
    args = parser.parse_args(argv)
    files = validate(args.config, args.secrets, args.ca)
    if args.target:
        install(args.target, files)
        print('Non-interactive configuration installed.')
    else:
        print('Non-interactive configuration validated.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit('Configuration staging failed: ' + str(error)) from None
