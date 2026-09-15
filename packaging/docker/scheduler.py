#!/usr/bin/env python3
"""Container entrypoint: one state writer, bounded passes, graceful shutdown."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from algosec_jira_bus.config import private_json

CONFIG = '/etc/algosec-jira-bus/bus.json'
STATE = '/var/lib/algosec-jira-bus'
SECRETS = '/etc/algosec-jira-bus/secrets.json'
HEALTH = '/var/lib/algosec-jira-bus/health.json'
MAX_HEALTH_AGE = 900


def utc_text(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')


def emit_event(event, **fields):
    """Emit one allowlisted scheduler event for machines and people to inspect."""
    now = time.time()
    record = {'time': now, 'time_utc': utc_text(now), 'event': event, **fields}
    print(json.dumps(record, sort_keys=True, separators=(',', ':')), flush=True)


def _write_private_json(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent,
                                              prefix='.' + path.name + '.')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, sort_keys=True, separators=(',', ':'))
            output.write('\n')
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


class Health:
    """Small persistent liveness summary; never contains configuration or credentials."""

    def __init__(self, path=HEALTH, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.value = {
            'schema': 1,
            'status': 'starting',
            'started_at': self.clock(),
            'updated_at': self.clock(),
            'consecutive_failures': 0,
            'last_success_at': None,
            'last_failure_at': None,
            'actions': {},
        }
        self.value['started_at_utc'] = utc_text(self.value['started_at'])

    def record(self, action, exit_code, duration_seconds, run_id):
        now = self.clock()
        self.value['updated_at'] = now
        self.value['updated_at_utc'] = utc_text(now)
        self.value['actions'][action] = {
            'exit_code': exit_code,
            'duration_seconds': round(duration_seconds, 3),
            'finished_at': now,
            'finished_at_utc': utc_text(now),
            'run_id': run_id,
        }
        if exit_code == 0:
            self.value['last_success_at'] = now
            self.value['last_success_at_utc'] = utc_text(now)
            self.value['consecutive_failures'] = 0
        else:
            self.value['last_failure_at'] = now
            self.value['last_failure_at_utc'] = utc_text(now)
            self.value['consecutive_failures'] += 1
        if self.value['actions'].get('doctor', {}).get('exit_code') not in (None, 0):
            self.value['status'] = 'not_ready'
        elif any(item.get('exit_code') != 0 for item in self.value['actions'].values()):
            self.value['status'] = 'degraded'
        else:
            self.value['status'] = 'healthy'
        _write_private_json(self.path, self.value)
        return dict(self.value)


def healthcheck(path=HEALTH, now=None):
    """Docker health probe: local, bounded and free of external API traffic."""
    try:
        value = private_json(Path(path), os.getuid(), max_bytes=64 * 1024)
    except (OSError, ValueError):
        return 1
    timestamp = value.get('updated_at') if isinstance(value, dict) else None
    current = time.time() if now is None else now
    if (value.get('schema') != 1 or value.get('status') != 'healthy'
            or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
            or timestamp > current + 60 or current - timestamp > MAX_HEALTH_AGE):
        return 1
    return 0


def load_secrets(path=SECRETS):
    try:
        values = private_json(Path(path), os.getuid(), max_bytes=64 * 1024)
    except ValueError:
        raise ValueError('secrets.json must be a private regular JSON file (0600)') from None
    if not isinstance(values, dict) or set(values) != {'JIRA_API_TOKEN', 'ASMS_API_PASSWORD'}:
        raise ValueError('secrets.json must contain JIRA_API_TOKEN and ASMS_API_PASSWORD only')
    for name, value in values.items():
        if not isinstance(value, str) or not value or any(c in value for c in '\r\n\0'):
            raise ValueError('Secret values must be nonempty single-line strings')
    os.environ.update(values)


class Runner:
    def __init__(self, health_path=HEALTH):
        self.stop = threading.Event()
        self.child = None
        self.health_path = health_path

    def shutdown(self, *_):
        self.stop.set()
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()

    def run(self, action, timeout):
        if self.stop.is_set():
            return 130
        command = ['algosec-jira-bus', '--config', CONFIG, '--state-dir', STATE, *action]
        self.child = subprocess.Popen(command)
        deadline = time.monotonic() + timeout
        try:
            while self.child.poll() is None:
                if self.stop.wait(0.2) or time.monotonic() >= deadline:
                    self.child.terminate()
                    try:
                        self.child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.child.kill()
                        self.child.wait()
                    return 130 if self.stop.is_set() else 124
            return self.child.returncode
        finally:
            self.child = None

    def observed(self, health, action, timeout):
        run_id = str(uuid.uuid4())
        emit_event('action_started', action=action, run_id=run_id)
        started = time.monotonic()
        code = self.run([action], timeout)
        duration = time.monotonic() - started
        health.record(action, code, duration, run_id)
        emit_event('action_finished', action=action, run_id=run_id,
                   exit_code=code, duration_seconds=round(duration, 3),
                   level='info' if code == 0 else 'error')
        return code

    def serve(self, poll_seconds=30, reconcile_seconds=86400):
        health = Health(path=self.health_path)
        if self.observed(health, 'doctor', 300):
            print('NOT READY: startup doctor failed; no polling started.', flush=True)
            return 1
        print('READY: startup doctor passed.', flush=True)
        next_reconcile = time.monotonic() + reconcile_seconds
        while not self.stop.is_set():
            code = self.observed(health, 'poll', 300)
            print(f'Poll finished: exit={code}', flush=True)
            if self.stop.is_set():
                break
            if time.monotonic() >= next_reconcile:
                code = self.observed(health, 'reconcile', 600)
                print(f'Reconcile finished: exit={code}', flush=True)
                next_reconcile = time.monotonic() + reconcile_seconds
            self.stop.wait(poll_seconds)
        return 0


def main():
    os.umask(0o077)
    args = sys.argv[1:]
    if args == ['healthcheck']:
        return healthcheck()
    load_secrets()
    runner = Runner()
    signal.signal(signal.SIGTERM, runner.shutdown)
    signal.signal(signal.SIGINT, runner.shutdown)
    if args and args != ['serve']:
        return runner.run(args, 600)
    return runner.serve()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        print('Container setup invalid: check mounted config, secrets.json and state permissions.', file=sys.stderr)
        raise SystemExit(1)
