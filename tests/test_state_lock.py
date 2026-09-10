"""Separate processes must share a lock before loading their state snapshots."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from algosec_jira_bus import bus
from algosec_jira_bus.sync import State
from test_bus import settings_for


class StateLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = settings_for(self.root)
        self.settings['journal'] = str(self.root / 'state')

    def test_second_process_loads_state_only_after_first_writer_finishes(self):
        script = '''
import json, sys
from algosec_jira_bus import bus
print('waiting', flush=True)
with bus.session(json.loads(sys.argv[1])) as (_, state, _, _):
    state.record('NET-2', {'change_request_id': 2})
'''
        with bus.session(self.settings) as (_, state, _, _):
            child = subprocess.Popen([sys.executable, '-B', '-c', script, json.dumps(self.settings)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: child.kill() if child.poll() is None else None)
            self.assertEqual(child.stdout.readline().strip(), 'waiting')
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
            state.record('NET-1', {'change_request_id': 1})
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(set(State(self.settings['state']).entries()), {'NET-1', 'NET-2'})

    def test_cli_directory_overrides_explicit_state_path(self):
        directory = self.root / 'override'
        _, state, _, _ = bus.build(self.settings, directory)
        self.assertEqual(state.path, directory / 'jira-sync.json')

    def test_corrupt_state_is_not_silently_treated_as_empty(self):
        state = State(self.root / 'state' / 'sync.json')
        state.path.write_text('{broken')
        with self.assertRaises(ValueError):
            State(state.path)

    def test_invalid_state_structure_is_rejected(self):
        state = State(self.root / 'state' / 'sync.json')
        for value in ([], {'issues': []}, {'failures': []}):
            state.path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                State(state.path)

    def test_state_file_must_be_private_and_cannot_be_a_link(self):
        state = State(self.root / 'state' / 'sync.json')
        state.record('NET-1', {'change_request_id': 1})
        state.path.chmod(0o640)
        with self.assertRaisesRegex(ValueError, 'owner-only'):
            State(state.path)
        state.path.chmod(0o600)
        link = state.path.with_name('linked.json')
        link.symlink_to(state.path)
        with self.assertRaisesRegex(ValueError, 'owner-only'):
            State(link)

    def test_state_lock_must_be_private_and_cannot_be_a_hardlink(self):
        path = self.root / 'state' / 'sync.json'
        with State.lock(path):
            pass
        lock = path.with_name(path.name + '.lock')
        hardlink = lock.with_name('other.lock')
        hardlink.hardlink_to(lock)
        with self.assertRaisesRegex(ValueError, 'private regular file'):
            with State.lock(path):
                pass
