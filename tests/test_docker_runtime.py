import importlib.util
import json
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('container_scheduler', Path(__file__).resolve().parents[1] / 'packaging/docker/scheduler.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
ROOT = Path(__file__).resolve().parents[1]


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.health_path = Path(self.temporary.name) / 'health.json'

    def runner(self):
        return m.Runner(health_path=self.health_path)

    def test_image_contains_only_the_self_contained_bus_runtime(self):
        dockerfile = (ROOT / 'packaging/docker/Dockerfile').read_text()
        self.assertIn('python:3.11.16-alpine3.23@sha256:', dockerfile)
        self.assertIn("apk add --upgrade --no-cache 'libuuid=2.41.6-r1'", dockerfile)
        self.assertIn('pip install --no-cache-dir --no-deps --no-build-isolation .', dockerfile)
        self.assertIn('/usr/local/lib/python3.11/site-packages/pip', dockerfile)
        self.assertIn('/usr/local/lib/python3.11/site-packages/setuptools', dockerfile)
        self.assertIn('/usr/local/lib/python3.11/site-packages/wheel', dockerfile)
        self.assertIn('/usr/local/lib/python3.11/site-packages/packaging', dockerfile)
        self.assertNotIn('vendor/', dockerfile)
        self.assertNotIn('COPY . .', dockerfile)
        for excluded in ('.git', 'tests', 'forge', 'docs'):
            self.assertNotIn('COPY ' + excluded, dockerfile)
        self.assertIn('USER 10001:10001', dockerfile)
        self.assertNotIn('EXPOSE', dockerfile)
        self.assertIn('HEALTHCHECK --interval=60s', dockerfile)
        self.assertIn('scheduler.py", "healthcheck"', dockerfile)

    def test_health_snapshot_records_success_and_failure_without_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'health.json'
            health = m.Health(path=path, clock=lambda: 1000.0)
            health.record('doctor', 0, 1.25, 'doctor-run')
            health.record('poll', 1, 2.5, 'poll-run')

            saved = json.loads(path.read_text())
            self.assertEqual(saved['status'], 'degraded')
            self.assertEqual(saved['consecutive_failures'], 1)
            self.assertEqual(saved['actions']['doctor']['exit_code'], 0)
            self.assertEqual(saved['actions']['poll']['run_id'], 'poll-run')
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertNotIn('token', path.read_text().lower())

            health.record('poll', 0, 0.5, 'poll-recovered')
            saved = json.loads(path.read_text())
            self.assertEqual(saved['status'], 'healthy')
            self.assertEqual(saved['consecutive_failures'], 0)
            self.assertEqual(saved['last_success_at'], 1000.0)

    def test_healthcheck_requires_fresh_healthy_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'health.json'
            path.write_text(json.dumps({'schema': 1, 'status': 'healthy',
                                        'updated_at': 1000.0}))
            path.chmod(0o600)
            self.assertEqual(m.healthcheck(path=path, now=1100.0), 0)
            self.assertEqual(m.healthcheck(path=path, now=2000.1), 1)
            path.write_text(json.dumps({'schema': 1, 'status': 'degraded',
                                        'updated_at': 1999.0}))
            self.assertEqual(m.healthcheck(path=path, now=2000.0), 1)

    def test_structured_scheduler_event_has_timestamp_and_run_id(self):
        with patch('builtins.print') as output:
            m.emit_event('action_finished', action='poll', run_id='run-1',
                         exit_code=0, duration_seconds=0.25)
        event = json.loads(output.call_args.args[0])
        self.assertEqual(event['event'], 'action_finished')
        self.assertEqual(event['action'], 'poll')
        self.assertEqual(event['run_id'], 'run-1')
        self.assertIn('time', event)
        self.assertTrue(event['time_utc'].endswith('Z'))

    def test_installer_applies_runtime_resource_limits(self):
        helper = (ROOT / 'packaging/docker/install-docker.sh').read_text()
        for expected in ('--pids-limit 128', '--memory 512m', '--memory-swap 512m'):
            self.assertIn(expected, helper)

    def test_private_json_secrets_preserve_shell_metacharacters(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            p = Path(d) / 'secrets.json'
            p.write_text(json.dumps({'JIRA_API_TOKEN': '$x`id`"\\', 'ASMS_API_PASSWORD': 'password'}))
            p.chmod(0o600)
            m.load_secrets(p)
            self.assertEqual(os.environ['JIRA_API_TOKEN'], '$x`id`"\\')
            p.chmod(0o644)
            with self.assertRaises(ValueError): m.load_secrets(p)

    def test_invalid_secret_shapes_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'secrets.json'
            for content in [[], {}, {'JIRA_API_TOKEN': 'x', 'ASMS_API_PASSWORD': ''}, {'JIRA_API_TOKEN': 'x\ny', 'ASMS_API_PASSWORD': 'x'}]:
                p.write_text(json.dumps(content)); p.chmod(0o600)
                with self.assertRaises(ValueError): m.load_secrets(p)

    def test_doctor_failure_blocks_poll(self):
        r = self.runner()
        with patch.object(r, 'run', return_value=1) as run:
            self.assertEqual(r.serve(), 1)
            run.assert_called_once_with(['doctor'], 300)

    def test_successful_doctor_emits_container_readiness_marker(self):
        runner = self.runner()
        with patch.object(runner, 'run', return_value=0), \
                patch.object(runner.stop, 'wait', side_effect=lambda _seconds: runner.stop.set()):
            with patch('builtins.print') as output:
                self.assertEqual(runner.serve(), 0)
        self.assertTrue(any(call.args and call.args[0] == 'READY: startup doctor passed.'
                            for call in output.call_args_list))

    def test_scheduler_serializes_poll_and_reconcile(self):
        r = self.runner(); actions = []
        def run(action, timeout):
            actions.extend(action)
            if action == ['reconcile']: r.stop.set()
            return 0
        with patch.object(r, 'run', side_effect=run):
            self.assertEqual(r.serve(poll_seconds=0, reconcile_seconds=0), 0)
        self.assertEqual(actions, ['doctor', 'poll', 'reconcile'])

    def test_default_poll_wait_is_30_seconds_after_completed_pass(self):
        runner = self.runner()
        actions = []
        def run(action, timeout):
            actions.extend(action)
            return 0
        def wait(seconds):
            self.assertEqual(actions, ['doctor', 'poll'])
            runner.stop.set()
            return True
        with patch.object(runner, 'run', side_effect=run), patch.object(runner.stop, 'wait', side_effect=wait) as paused:
            self.assertEqual(runner.serve(), 0)
        paused.assert_called_once_with(30)

    def test_shutdown_prevents_new_children(self):
        r = self.runner(); r.shutdown(signal.SIGTERM, None)
        with patch.object(m.subprocess, 'Popen') as popen:
            self.assertEqual(r.run(['poll'], 300), 130)
            popen.assert_not_called()

    def test_timeout_terminates_then_kills_child(self):
        r = self.runner()
        child = Mock(); child.poll.return_value = None
        child.wait.side_effect = [m.subprocess.TimeoutExpired('cmd', 10), 0]
        with patch.object(m.subprocess, 'Popen', return_value=child), patch.object(r.stop, 'wait', return_value=False):
            self.assertEqual(r.run(['poll'], 0), 124)
        child.terminate.assert_called_once(); child.kill.assert_called_once()
        self.assertIsNone(r.child)

    def test_cli_never_forces_apply(self):
        child = Mock(); child.poll.return_value = 0; child.returncode = 0
        with patch.object(m.subprocess, 'Popen', return_value=child) as popen:
            self.assertEqual(self.runner().run(['poll'], 300), 0)
            self.assertNotIn('--apply', popen.call_args.args[0])
