import tempfile
import unittest
from algosec_jira_bus.bus import build

class TransportTimeout(unittest.TestCase):
    def test_configured_timeout_reaches_transport(self):
        with tempfile.TemporaryDirectory() as root:
            ff, *_ = build({'fireflow': {'request_timeout': 45}}, root)
            self.assertEqual(ff.request.keywords['timeout'], 45)

    def test_boolean_timeout_is_invalid(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                build({'fireflow': {'request_timeout': True}}, root)
