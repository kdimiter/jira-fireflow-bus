import unittest


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
        with patch.object(self.wizard, 'ask', side_effect=['https://asms.test', '', 'bot']), \
             patch.object(self.wizard.getpass, 'getpass', return_value='secret'), \
             patch.dict('sys.modules', {'algosec_jira_bus.provision': None}):
            self.assertEqual(self.wizard.collect_fireflow_credentials(), ('https://asms.test', 'bot', 'secret', ''))

    def test_bad_pin_stops_before_secret_or_network(self):
        from unittest.mock import patch
        with patch.object(self.wizard, 'ask', side_effect=['https://asms.test', 'invalid']), \
             patch.object(self.wizard.getpass, 'getpass') as secret, \
             patch.dict('sys.modules', {'algosec_jira_bus.provision': None}):
            with self.assertRaises(ValueError): self.wizard.collect_fireflow_credentials()
            secret.assert_not_called()
