"""Publication guard must catch private material without echoing it to public logs."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / 'scripts/check-public-tree.py'


class PublicTreeScannerTests(unittest.TestCase):
    def scan(self, files):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            return subprocess.run([sys.executable, str(SCANNER), str(root)],
                                  capture_output=True, text=True)

    def test_placeholders_and_documentation_addresses_are_allowed(self):
        result = self.scan({'README.md': 'https://your-tenant.atlassian.net 192.0.2.10 api@example.com'})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_markers_are_categorized_without_echoing_values(self):
        email = 'person@' + 'gmail' + '.com'
        tenant = 'real-' + 'tenant' + '.atlassian.net'
        address = '.'.join(('10', '20', '30', '40'))
        result = self.scan({'notes.md': ' '.join((email, tenant, address))})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('personal email address', result.stderr)
        self.assertIn('non-placeholder Jira tenant', result.stderr)
        self.assertIn('private IPv4 address', result.stderr)
        for value in (email, tenant, address):
            self.assertNotIn(value, result.stderr)

    def test_runtime_secret_file_is_rejected(self):
        result = self.scan({'config/secrets.json': '{}'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('forbidden public artifact', result.stderr)

    def test_symbolic_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'target.txt'
            target.write_text('safe')
            (root / 'link.txt').symlink_to(target)
            result = subprocess.run([sys.executable, str(SCANNER), str(root)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('symbolic links are not allowed', result.stderr)


if __name__ == '__main__':
    unittest.main()
