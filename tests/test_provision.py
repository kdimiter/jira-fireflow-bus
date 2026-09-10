import unittest

from algosec_jira_bus.provision import ProvisionError, create_asms_user


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
