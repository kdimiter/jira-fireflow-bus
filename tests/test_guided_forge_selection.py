"""Contract tests for selecting an existing Forge app in guided setup."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUIDED = ROOT / 'scripts/guided-linux-setup.sh'


class GuidedForgeSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = GUIDED.read_text()

    def test_discovers_installed_apps_from_paginated_jira_custom_fields(self):
        self.assertIn('/rest/api/3/field/search', self.script)
        self.assertIn("'type': 'custom'", self.script)
        self.assertIn("'startAt': start_at", self.script)
        self.assertIn("'maxResults': 100", self.script)
        self.assertIn('/static/algosec-network-access', self.script)
        self.assertRegex(self.script, re.compile(r'PRODUCTION', re.IGNORECASE))

    def test_menu_offers_each_existing_app_and_a_new_app(self):
        self.assertIn("existing:", self.script)
        self.assertRegex(
            self.script,
            re.compile(r"choose[^\n]*'Forge application'|--title ['\"]Forge application", re.DOTALL),
        )
        self.assertRegex(
            self.script,
            re.compile(r'APP_MODE=register.*case\s+"\$APP_CHOICE".*new\)', re.DOTALL),
        )

    def test_existing_selection_skips_forge_deploy_and_pins_jira_preparation(self):
        self.assertRegex(
            self.script,
            re.compile(
                r'existing:\*\).*?if\s+\[\s+"\$CANDIDATE_ENV"\s+=\s+PRODUCTION\s+\]'
                r'.*?SKIP_FORGE=1',
                re.DOTALL,
            ),
        )
        self.assertRegex(
            self.script,
            re.compile(
                r'if\s+\[\s+"\$SKIP_FORGE"\s+=\s+1\s+\].*?else.*?setup-forge\.sh',
                re.DOTALL,
            ),
        )
        self.assertRegex(
            self.script,
            re.compile(r'prepare-jira\.sh.*--forge-app-id\s+"\$APP_ID"', re.DOTALL),
        )

    def test_does_not_depend_on_a_nonexistent_forge_apps_list_command(self):
        self.assertNotIn('forge apps list', self.script)


if __name__ == '__main__':
    unittest.main()
