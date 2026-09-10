#!/usr/bin/env python3
"""Container entrypoint: one state writer, bounded passes, graceful shutdown."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from algosec_jira_bus.config import private_json

CONFIG = '/etc/algosec-jira-bus/bus.json'
STATE = '/var/lib/algosec-jira-bus'
SECRETS = '/etc/algosec-jira-bus/secrets.json'


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
    def __init__(self):
        self.stop = threading.Event()
        self.child = None

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

    def serve(self, poll_seconds=30, reconcile_seconds=86400):
        if self.run(['doctor'], 300):
            print('NOT READY: startup doctor failed; no polling started.', flush=True)
            return 1
        next_reconcile = time.monotonic() + reconcile_seconds
        while not self.stop.is_set():
            code = self.run(['poll'], 300)
            print(f'Poll finished: exit={code}', flush=True)
            if self.stop.is_set():
                break
            if time.monotonic() >= next_reconcile:
                code = self.run(['reconcile'], 600)
                print(f'Reconcile finished: exit={code}', flush=True)
                next_reconcile = time.monotonic() + reconcile_seconds
            self.stop.wait(poll_seconds)
        return 0


def main():
    os.umask(0o077)
    load_secrets()
    runner = Runner()
    signal.signal(signal.SIGTERM, runner.shutdown)
    signal.signal(signal.SIGINT, runner.shutdown)
    args = sys.argv[1:]
    if args and args != ['serve']:
        return runner.run(args, 600)
    return runner.serve()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        print('Container setup invalid: check mounted config, secrets.json and state permissions.', file=sys.stderr)
        raise SystemExit(1)
