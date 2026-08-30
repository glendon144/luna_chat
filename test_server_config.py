import unittest
from unittest.mock import patch

import app


class ServerConfigTests(unittest.TestCase):
    @patch.object(app.sys, "frozen", True, create=True)
    @patch.object(app.sys, "platform", "darwin")
    def test_frozen_app_uses_persistent_macos_data_directory(self):
        self.assertEqual(app.default_data_dir(), app.Path.home() / "Library" / "Application Support" / "Luna Chat")

    def test_default_is_loopback_only(self):
        self.assertEqual(app.server_config([], {}), ("127.0.0.1", 5000, False))

    @patch("app.private_lan_addresses", return_value=["192.168.0.42"])
    def test_share_lan_uses_the_only_private_address(self, _addresses):
        self.assertEqual(app.server_config(["--share-lan"], {}), ("192.168.0.42", 5000, False))

    @patch("app.private_lan_addresses", return_value=["192.168.0.42", "192.168.1.42"])
    def test_share_lan_requires_an_explicit_address_when_multiple_exist(self, _addresses):
        with self.assertRaises(SystemExit):
            app.server_config(["--share-lan"], {})

    @patch("app.private_lan_addresses", return_value=["192.168.0.42"])
    def test_rejects_wildcard_address(self, _addresses):
        with self.assertRaises(SystemExit):
            app.server_config(["--share-lan", "--host", "0.0.0.0"], {})


if __name__ == "__main__":
    unittest.main()
