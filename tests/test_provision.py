import unittest

from algosec_jira_bus.provision import (ProvisionError, create_asms_user,
                                        list_fireflow_device_tree_names)


class FireFlowProvisionTests(unittest.TestCase):
    def call(self, request, **changes):
        arguments = dict(
            transport={'base_url': 'https://asms.example.test'},
            admin_username='admin', admin_password='bootstrap-secret',
            username='jira_bus_api', password='service-secret',
            email='bot@example.test', request=request)
        arguments.update(changes)
        return create_asms_user(**arguments)

    def test_documented_permissions_and_cookie(self):
        calls = []
        def request(config, path, **kwargs):
            calls.append((path, kwargs))
            if len(calls) == 1:
                return {'status': True, 'SessionID': 'opaque-session'}
            return {'status': 'OK', 'successUsers': [{'username': 'jira_bus_api'}]}
        result = self.call(request)
        self.assertNotIn('secret', str(result))
        self.assertEqual(calls[1][0], '/afa/api/v1/users')
        self.assertEqual(calls[1][1]['headers'],
                         {'Cookie': 'PHPSESSID=opaque-session'})
        body = calls[1][1]['body']
        self.assertEqual(body['administrator'], 'yes')
        self.assertEqual(body['fireflowAdmin'], 'yes')
        self.assertEqual(body['authorizedDevices'][0], {
            'id': 'ALL_FIREWALLS', 'displayName': 'ALL_FIREWALLS',
            'profile': 'Standard', 'notification': 'no'})

    def test_failed_login_never_creates(self):
        calls = []
        def request(*args, **kwargs):
            calls.append((args, kwargs))
            return {'status': False, 'SessionID': 'anything'}
        with self.assertRaises(ProvisionError):
            self.call(request)
        self.assertEqual(len(calls), 1)

    def test_mutation_timeout_is_not_retried_and_hides_secrets(self):
        calls = []
        def request(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                return {'status': True, 'SessionID': 'session'}
            raise RuntimeError('service-secret bootstrap-secret')
        with self.assertRaises(ProvisionError) as caught:
            self.call(request)
        self.assertEqual(len(calls), 2)
        self.assertIn('unknown', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_untrusted_origin_and_cookie_injection_are_rejected(self):
        with self.assertRaises(ProvisionError):
            self.call(lambda *_a, **_k: self.fail('network must not run'),
                      transport={'base_url': 'http://asms.example.test'})
        with self.assertRaises(ProvisionError):
            self.call(lambda *_a, **_k:
                      {'status': True, 'SessionID': 'bad; injected=value'})

    def test_device_tree_names_come_from_fireflow_authenticated_allowed_devices(self):
        calls = []
        def request(config, path, **kwargs):
            calls.append((path, kwargs))
            if path == '/FireFlow/api/authentication/authenticate':
                return {
                    'status': 'Success', 'messages': [{'code': 'success'}],
                    'data': {
                        'sessionId': 'fireflow-session',
                        'phpSessionId':
                            'PHPSESSID=php-session; path=/; secure; HttpOnly',
                    },
                }
            if path == '/afa/api/v1/allowedDevices':
                return [
                    {'treeName': 'parent_fw_b', 'displayName': 'FW B'},
                    {'treeName': 'fw_a', 'displayName': 'FW A'},
                    {'treeName': 'fw_a', 'displayName': 'FW A duplicate'},
                ]
            self.fail(path)

        names = list_fireflow_device_tree_names(
            {'base_url': 'https://asms.example.test'}, 'jira_bus_api',
            'service-secret', request=request)

        self.assertEqual(names, ['fw_a', 'parent_fw_b'])
        self.assertEqual(calls[0][0],
                         '/FireFlow/api/authentication/authenticate')
        self.assertEqual(calls[0][1]['body'], {
            'username': 'jira_bus_api', 'password': 'service-secret'})
        self.assertEqual(calls[1], (
            '/afa/api/v1/allowedDevices', {
                'headers': {'Cookie': 'PHPSESSID=php-session'},
                'query': {'domain': 0, 'includeBlueCoat': 'no',
                          'onlyFireflowSupportedDevices': 'yes'},
            }))

    def test_device_discovery_accepts_current_bare_php_session_format(self):
        calls = []
        def request(config, path, **kwargs):
            calls.append((path, kwargs))
            if path == '/FireFlow/api/authentication/authenticate':
                return {
                    'status': 'Success', 'messages': [],
                    'data': {'sessionId': 'fireflow-session',
                             'phpSessionId': 'bare-php-session'},
                }
            return [{'treeName': 'fw_a'}]

        self.assertEqual(list_fireflow_device_tree_names(
            {'base_url': 'https://asms.example.test'}, 'user', 'password',
            request=request), ['fw_a'])
        self.assertEqual(calls[1][1]['headers'], {
            'Cookie': 'PHPSESSID=bare-php-session'})

    def test_device_discovery_stops_on_fireflow_authentication_failure(self):
        calls = []
        def request(config, path, **kwargs):
            calls.append(path)
            return {'status': 'Failure', 'messages': [
                {'code': 'authentication.failure'}], 'data': None}

        with self.assertRaisesRegex(
                ProvisionError, 'first login|password') as caught:
            list_fireflow_device_tree_names(
                {'base_url': 'https://asms.example.test'}, 'jira_bus_api',
                'service-secret', request=request)
        self.assertEqual(calls, ['/FireFlow/api/authentication/authenticate'])
        self.assertNotIn('service-secret', str(caught.exception))

    def test_device_discovery_rejects_malformed_or_empty_lists(self):
        auth = {
            'status': 'Success', 'messages': [],
            'data': {'sessionId': 'ff-session',
                     'phpSessionId': 'PHPSESSID=php-session; path=/'},
        }
        for devices in ([], [{}], [{'treeName': 'bad\x7fname'}]):
            with self.subTest(devices=devices):
                replies = iter((auth, devices))
                with self.assertRaises(ProvisionError):
                    list_fireflow_device_tree_names(
                        {'base_url': 'https://asms.example.test'}, 'user',
                        'password', request=lambda *_a, **_kw: next(replies))


class WizardProvisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location('provision_wizard', Path(__file__).parents[1] / 'scripts/setup_wizard.py')
        cls.wizard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.wizard)

    def test_existing_user_is_collected_without_admin_provisioning(self):
        from unittest.mock import patch
        with patch.object(self.wizard, 'ask', side_effect=['https://asms.test', 'N', 'bot']), \
             patch.object(self.wizard.getpass, 'getpass', return_value='secret'), \
             patch.dict('sys.modules', {'algosec_jira_bus.provision': None}):
            self.assertEqual(self.wizard.collect_fireflow_credentials(), ('https://asms.test', 'bot', 'secret', '', False))

    def test_bad_pin_stops_before_secret_or_network(self):
        from unittest.mock import patch
        with patch.object(self.wizard, 'ask', side_effect=['https://asms.test', 'y', 'NO']), \
             patch.object(self.wizard, 'capture_certificate_sha256', return_value='0' * 64), \
             patch.object(self.wizard.getpass, 'getpass') as secret, \
             patch.dict('sys.modules', {'algosec_jira_bus.provision': None}):
            with self.assertRaisesRegex(ValueError, 'not trusted'):
                self.wizard.collect_fireflow_credentials()
            secret.assert_not_called()


class ProvisionCliTests(unittest.TestCase):
    def test_password_mismatch_stops_before_user_creation(self):
        from unittest.mock import patch
        from algosec_jira_bus import provision
        with patch('builtins.input', side_effect=[
                'admin', 'jira_bus_api_137', 'bot137@example.test']), \
             patch.object(provision.getpass, 'getpass', side_effect=[
                 'admin-secret', 'first-password', 'second-password',
                 'first-password', 'second-password', 'first-password', 'second-password']), \
             patch.object(provision, '_request', return_value={
                 'status': True, 'SessionID': 'verified-session'}), \
             patch.object(provision, 'create_asms_user') as create:
            self.assertEqual(provision.main([
                '--base-url', 'https://asms.example.test', '--apply']), 2)
            create.assert_not_called()


class ProvisionInputRegressionTests(unittest.TestCase):
    def test_password_mismatch_can_be_corrected_before_user_creation(self):
        from unittest.mock import patch
        from algosec_jira_bus import provision
        with patch('builtins.input', side_effect=[
                'admin', 'jira_bus_api', 'bot@example.test', 'CREATE']), \
             patch.object(provision.getpass, 'getpass', side_effect=[
                 'admin-secret', 'typo', 'different', 'correct', 'correct']), \
             patch.object(provision, '_request', return_value={
                 'status': True, 'SessionID': 'verified-session'}), \
             patch.object(provision, 'create_asms_user', return_value={}) as create:
            self.assertEqual(provision.main([
                '--base-url', 'https://asms.example.test', '--apply']), 0)
            self.assertEqual(create.call_args.args[4], 'correct')
            create.assert_called_once()

    def test_delete_control_character_is_rejected_before_network(self):
        with self.assertRaisesRegex(ProvisionError, 'control'):
            FireFlowProvisionTests().call(
                lambda *_a, **_kw: self.fail('must not send DEL to server'),
                admin_password='mistake\x7fsecret')

    def test_safe_login_diagnostics_never_include_exception_secrets(self):
        import socket
        import ssl
        import urllib.error
        cases = [
            (urllib.error.URLError(ssl.SSLCertVerificationError('bootstrap-secret')), 'certificate'),
            (TimeoutError('bootstrap-secret'), 'timed out'),
            (urllib.error.URLError(socket.gaierror('bootstrap-secret')), 'DNS'),
            (ConnectionRefusedError('bootstrap-secret'), 'connect'),
            (urllib.error.HTTPError('https://bootstrap-secret@example.test', 403, 'bootstrap-secret', {}, None), '401/403'),
            (urllib.error.HTTPError('https://bootstrap-secret@example.test', 500, 'bootstrap-secret', {}, None), 'HTTP 500'),
            (ValueError('bootstrap-secret'), 'response'),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected):
                calls = []
                def request(*args, **kwargs):
                    calls.append((args, kwargs))
                    raise error
                with self.assertRaises(ProvisionError) as caught:
                    FireFlowProvisionTests().call(request)
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('bootstrap-secret', str(caught.exception))
                self.assertEqual(len(calls), 1)

    def test_real_terminal_editing_masks_secrets_and_restores_settings(self):
        import os
        import pty
        import select
        import subprocess
        import sys
        import termios
        import time
        # The same key sequences must work independently of the PTY erase setting.
        for erase in (b'\x08', b'\x7f'):
            for secret in (False, True):
                for entered, expected in ((b'abc\x08Z\x7fQ\n', 'abQ'),
                                          ('помилка\x15пароль\n'.encode(), 'пароль'),
                                          (b'ac\x1b[Db\x1b[Cd\x1bOHX\x1bOFY\n', 'XabcdY'),
                                          (b'ac\x1bODb\x1bOCd\x1b[HX\x1b[FY\n', 'XabcdY'),
                                          (b'abcd\x1b[1~X\x1b[4~Y\n', 'XabcdY'),
                                          (b'abcd\x1b[D\x08\x1b[3~X\n', 'abX'),
                                          (b'abcd\x1b[D\x7f\x1b[3~X\n', 'abX'),
                                          (b'ab\x1b[A\x1b[B\x1bOA\x1bOBc\n', 'abc'),
                                          ('ак\x1b[Dб\n'.encode(), 'абк'),
                                          (b'abcd\x1b[D\x1b[D\n', 'abcd'),
                                          (b'abc\x03', None),
                                          (b'abc\x04', 'cancelled')):
                    with self.subTest(erase=erase, secret=secret, entered=entered):
                        master, slave = pty.openpty()
                        original = termios.tcgetattr(slave)
                        original[6][termios.VERASE] = erase
                        termios.tcsetattr(slave, termios.TCSANOW, original)
                        original = termios.tcgetattr(slave)
                        program = (
                            'from algosec_jira_bus.provision import ConsoleError, _prompt\n'
                            'try:\n'
                            f' value = _prompt("PROMPT: ", secret={secret!r})\n'
                            f' print("MATCH", value == {expected!r}, flush=True)\n'
                            'except KeyboardInterrupt:\n'
                            ' print("INTERRUPTED", flush=True)\n'
                            'except ConsoleError as error:\n'
                            ' print("CANCELLED", str(error), flush=True)\n')
                        child = subprocess.Popen([sys.executable, '-c', program],
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True)
                        output = b''
                        try:
                            deadline = time.monotonic() + 5
                            while b'PROMPT: ' not in output and time.monotonic() < deadline:
                                if select.select([master], [], [], .1)[0]:
                                    output += os.read(master, 4096)
                                if child.poll() is not None:
                                    break
                            self.assertIn(b'PROMPT: ', output)
                            os.write(master, entered)
                            while child.poll() is None and time.monotonic() < deadline:
                                if select.select([master], [], [], .1)[0]:
                                    output += os.read(master, 4096)
                            self.assertIsNotNone(child.poll(), 'prompt hung')
                            while select.select([master], [], [], .05)[0]:
                                output += os.read(master, 4096)
                            if expected == 'cancelled':
                                self.assertIn(b'CANCELLED Input cancelled.', output)
                                self.assertNotIn(b'Traceback', output)
                            else:
                                self.assertIn(b'INTERRUPTED' if expected is None else b'MATCH True', output)
                            restored = termios.tcgetattr(slave)
                            # BSD sets the dynamic PENDIN status when canonical
                            # mode resumes; compare every configured setting.
                            restored[3] &= ~getattr(termios, 'PENDIN', 0)
                            original[3] &= ~getattr(termios, 'PENDIN', 0)
                            self.assertEqual(restored, original)
                            if secret:
                                self.assertIn(b'*', output)
                                self.assertNotIn(b'abc', output)
                                self.assertNotIn('пароль'.encode(), output)
                                self.assertNotIn('абк'.encode(), output)
                                if entered == b'abcd\x1b[D\x1b[D\n':
                                    self.assertIn(b'PROMPT: ****\x1b[2D', output)
                            elif entered == b'abcd\x1b[D\x1b[D\n':
                                self.assertIn(b'PROMPT: abcd\x1b[2D', output)
                        finally:
                            if child.poll() is None:
                                child.kill()
                            child.wait()
                            os.close(master)
                            os.close(slave)

    def test_secret_fallback_refuses_getpass_echo(self):
        import getpass
        import warnings
        from unittest.mock import patch
        from algosec_jira_bus import provision
        def no_terminal(*_args, **_kwargs):
            warnings.warn('Cannot control echo', getpass.GetPassWarning)
            return 'would-be-echoed'
        with patch('sys.stdin.isatty', return_value=False), \
             patch.object(provision.getpass, 'getpass', side_effect=no_terminal):
            with self.assertRaisesRegex(ValueError, 'terminal'):
                provision._prompt('Secret: ', secret=True)


class ProvisionEarlyLoginTests(unittest.TestCase):
    def test_bad_admin_login_stops_before_integration_prompts(self):
        from unittest.mock import patch
        from algosec_jira_bus import provision
        prompts = []
        def prompt(label, **kwargs):
            prompts.append(label)
            if label == 'Existing ASMS administrator username: ':
                return 'admin'
            if label == 'Existing ASMS administrator password: ':
                return 'admin-secret'
            self.fail('Must authenticate before collecting integration details')
        with patch.object(provision, '_prompt', side_effect=prompt), \
             patch.object(provision, '_request', return_value={
                 'status': False, 'message': 'remote-private-details'}) as request:
            with self.assertRaisesRegex(ProvisionError, 'credentials rejected') as caught:
                provision.main(['--base-url', 'https://asms.example.test', '--apply'])
        self.assertNotIn('remote-private-details', str(caught.exception))
        self.assertEqual(len(prompts), 2)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], '/fa/server/connection/login')

    def test_successful_wizard_authenticates_once_before_new_user_prompts(self):
        import contextlib
        import io
        from unittest.mock import patch
        from algosec_jira_bus import provision
        events = []
        values = iter(['admin', 'admin-secret', 'jira_bus_api', 'bot@example.test',
                       'service-secret', 'service-secret', 'CREATE'])
        def prompt(label, **kwargs):
            events.append(('prompt', label))
            return next(values)
        def request(config, path, **kwargs):
            events.append(('request', path))
            if path == '/fa/server/connection/login':
                return {'status': True, 'SessionID': 'private-session'}
            self.assertEqual(kwargs['headers'], {'Cookie': 'PHPSESSID=private-session'})
            return {'status': 'OK', 'successUsers': [{'username': 'jira_bus_api'}]}
        output = io.StringIO()
        with patch.object(provision, '_prompt', side_effect=prompt), \
             patch.object(provision, '_request', side_effect=request), \
             contextlib.redirect_stdout(output):
            self.assertEqual(provision.main([
                '--base-url', 'https://asms.example.test', '--apply']), 0)
        self.assertEqual(events[2], ('request', '/fa/server/connection/login'))
        self.assertEqual(events[3], ('prompt', 'New integration username [jira_bus_api]: '))
        self.assertEqual([event for event in events if event[0] == 'request'], [
            ('request', '/fa/server/connection/login'), ('request', '/afa/api/v1/users')])
        self.assertNotIn('private-session', output.getvalue())
        self.assertNotIn('secret', output.getvalue())


class ProvisionCancellationTests(unittest.TestCase):
    def test_non_terminal_eof_is_a_safe_cancellation(self):
        from unittest.mock import patch
        from algosec_jira_bus.console import ConsoleError, prompt
        for secret in (False, True):
            with self.subTest(secret=secret), \
                 patch('sys.stdin.isatty', return_value=False), \
                 patch('builtins.input', side_effect=EOFError), \
                 patch('getpass.getpass', side_effect=EOFError):
                with self.assertRaisesRegex(ConsoleError, r'^Input cancelled\.$'):
                    prompt('Input: ', secret=secret)


class ProvisionNarrowTerminalTests(unittest.TestCase):
    def test_long_fields_scroll_without_wrapping_or_exposing_secrets(self):
        import fcntl
        import os
        import pty
        import re
        import select
        import struct
        import subprocess
        import sys
        import termios
        import time
        import unicodedata
        for secret in (False, True):
            for label in ('TOKEN: ', 'TOKEN: ' + 'long label ' * 8):
                with self.subTest(secret=secret, label=label):
                    base = 's3cretValue' * 12 if secret else '世界е\u0301' * 35
                    expected = 'X' + base[0] + base[2:-2] + '!' + base[-1]
                    entered = (base + '\x1b[HX\x1b[C\x1b[3~\x1b[F\x1b[D\x08!\n').encode()
                    master, slave = pty.openpty()
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 40, 0, 0))
                    program = ('from algosec_jira_bus.console import prompt\n'
                               f'value = prompt({label!r}, secret={secret!r})\n'
                               f'print("MATCH", value == {expected!r}, flush=True)\n')
                    child = subprocess.Popen([sys.executable, '-c', program],
                        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
                    output = b''
                    try:
                        deadline = time.monotonic() + 8
                        while b'TOKEN:' not in output and time.monotonic() < deadline:
                            if select.select([master], [], [], .1)[0]:
                                output += os.read(master, 65536)
                        self.assertIn(b'TOKEN:', output)
                        os.write(master, entered)
                        while child.poll() is None and time.monotonic() < deadline:
                            if select.select([master], [], [], .1)[0]:
                                output += os.read(master, 65536)
                        self.assertIsNotNone(child.poll(), 'editor hung')
                        while select.select([master], [], [], .05)[0]:
                            output += os.read(master, 65536)
                        self.assertIn(b'MATCH True', output)
                        text = output.decode()
                        segments = text.split('\r\x1b[2K')
                        for segment in segments:
                            row = segment.split('\r')[0].split('\n')[0]
                            match = re.fullmatch(r'(.*?)(?:\x1b\[(\d+)D)?', row)
                            self.assertIsNotNone(match)
                            visible, back = match.groups()
                            width = sum(0 if unicodedata.combining(c) else
                                        2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
                                        for c in visible)
                            self.assertLess(width, 40, 'redraw would wrap at terminal width')
                            self.assertGreaterEqual(width - int(back or 0), 0)
                            self.assertLess(width - int(back or 0), 40)
                        if secret:
                            self.assertNotIn('s3cret', text)
                            self.assertNotIn('Value', text)
                    finally:
                        if child.poll() is None:
                            child.kill()
                        child.wait()
                        os.close(master)
                        os.close(slave)


class ProvisionSuccessOutputTests(unittest.TestCase):
    def test_success_never_prints_creation_return_payload_or_credentials(self):
        import contextlib
        import io
        from unittest.mock import patch
        from algosec_jira_bus import provision
        values = ['admin-account', 'admin-secret', 'integration-user',
                  'private-email@example.test', 'service-secret', 'service-secret', 'CREATE']
        payload = {'username': values[2], 'email': values[3], 'password': values[4],
                   'session': 'private-session', 'extra': 'private-return-payload'}
        output = io.StringIO()
        with patch.object(provision, '_prompt', side_effect=values), \
             patch.object(provision, 'authenticate_asms', return_value='private-session'), \
             patch.object(provision, 'create_asms_user', return_value=payload), \
             contextlib.redirect_stdout(output):
            self.assertEqual(provision.main([
                '--base-url', 'https://asms.example.test', '--apply']), 0)
        self.assertEqual(output.getvalue(),
            'ASMS administrator login verified.\n'
            'Account created with a temporary password. Do not rerun this helper.\n'
            'Sign in once at the FireFlow web interface, replace the temporary '
            'password, then enter the new password in bus_conf.\n')
        for private_value in values[:-1] + list(payload.values()):
            self.assertNotIn(private_value, output.getvalue())
