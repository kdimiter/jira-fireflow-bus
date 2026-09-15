from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.helper = (ROOT / 'packaging/docker/bus_diag').read_text()
        self.installer = (ROOT / 'packaging/docker/install-docker.sh').read_text()

    def test_helper_offers_status_doctor_logs_events_and_report(self):
        for command in ('status', 'doctor', 'logs', 'events', 'report'):
            self.assertIn(command + ')', self.helper)
        self.assertIn('docker logs --timestamps', self.helper)
        self.assertIn('jira-bus.jsonl', self.helper)
        self.assertIn('health.json', self.helper)

    def test_helper_never_reads_or_prints_the_secrets_file(self):
        self.assertNotIn('secrets.json', self.helper)
        self.assertNotIn('JIRA_API_TOKEN', self.helper)
        self.assertNotIn('ASMS_API_PASSWORD', self.helper)

    def test_collected_report_cannot_overwrite_an_existing_path(self):
        self.assertIn('set -C', self.helper)
        self.assertIn('exec 3>"$output"', self.helper)
        self.assertNotIn('mv "$temporary" "$output"', self.helper)

    def test_installer_installs_the_diagnostics_helper(self):
        self.assertIn('BUS_DIAG_SOURCE', self.installer)
        self.assertIn('/usr/local/sbin/bus_diag', self.installer)


if __name__ == '__main__':
    unittest.main()
