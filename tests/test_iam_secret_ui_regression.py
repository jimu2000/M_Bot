"""Regression checks for one-time IAM credential rendering."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IamSecretUiRegressionTests(unittest.TestCase):
    def test_bot_user_renders_pem_and_config_together(self):
        js = (ROOT / "web/static/js/accounts.js").read_text()
        start = js.index("async function createBotUser()")
        end = js.index("async function showUserKeys", start)
        body = js[start:end]
        self.assertIn("showOnceSecrets([", body)
        self.assertIn('filename: "oci_api_key.pem"', body)
        self.assertIn('filename: "oci_config.txt"', body)
        self.assertNotIn("showOnceSecret(\"Bot API 用户私钥\"", body)

    def test_add_api_key_renders_pem_and_config_together(self):
        js = (ROOT / "web/static/js/accounts.js").read_text()
        start = js.index("async function addUserKey(")
        end = js.index("async function deleteUserKey", start)
        body = js[start:end]
        self.assertIn("showOnceSecrets([", body)
        self.assertIn('filename: "oci_api_key.pem"', body)
        self.assertIn('filename: "oci_config.txt"', body)

    def test_secret_panel_has_mobile_layout_rules(self):
        css = (ROOT / "web/static/css/base.css").read_text()
        self.assertIn(".iam-secret-actions", css)
        self.assertIn("@media (max-width: 640px)", css)


if __name__ == "__main__":
    unittest.main()
