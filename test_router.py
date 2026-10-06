import json
import os
import tempfile
import unittest
from unittest import mock

import codex_router
import setup


class QuotaDiagnosticsTests(unittest.TestCase):
    def test_quota_headers_exclude_sensitive_values(self):
        headers = [
            ("x-ratelimit-remaining", "12"),
            ("x-ratelimit-reset", "123456"),
            ("authorization", "Bearer secret"),
            ("set-cookie", "private"),
        ]
        self.assertEqual(
            codex_router.quota_response_headers(headers),
            ["x-ratelimit-remaining=12", "x-ratelimit-reset=123456"],
        )

    def test_quota_payload_logs_only_matching_fields(self):
        payload = json.dumps({
            "ordinaryUsageAllowed": False,
            "rateLimits": {
                "primary": {"usedPercent": 100, "resetsAt": 123456},
                "accountId": "secret-account",
            },
            "message": "private prompt text",
        }).encode()
        summary = codex_router.quota_payload_summary(payload)
        joined = " ".join(summary)
        self.assertIn("ordinaryUsageAllowed=False", joined)
        self.assertIn("rateLimits.primary.usedPercent=100", joined)
        self.assertIn("rateLimits.primary.resetsAt=123456", joined)
        self.assertNotIn("private prompt text", joined)
        self.assertNotIn("secret-account", joined)

    def test_quota_probe_paths(self):
        self.assertTrue(codex_router.quota_probe_path("/wham/usage"))
        self.assertTrue(codex_router.quota_probe_path("/api/codex/accounts/check"))
        self.assertFalse(codex_router.quota_probe_path("/responses"))


class OfficialCredentialTests(unittest.TestCase):
    def test_vendor_key_prefers_credential_manager(self):
        with mock.patch.object(codex_router, "read_credential", return_value="stored-key"):
            self.assertEqual(codex_router.load_key("ZAI", "MISSING_TEST_ENV"), "stored-key")

    def test_engine_credentials_are_preserved_without_auth_file_lookup(self):
        incoming = [
            ("Host", "127.0.0.1:8231"),
            ("Content-Length", "10"),
            ("Authorization", "Bearer engine-token"),
            ("chatgpt-account-id", "engine-account"),
        ]
        with mock.patch.object(codex_router, "chatgpt_auth") as fallback:
            headers = codex_router.official_request_headers(incoming, "chatgpt.com")
        fallback.assert_not_called()
        self.assertEqual(headers["Authorization"], "Bearer engine-token")
        self.assertEqual(headers["chatgpt-account-id"], "engine-account")
        self.assertEqual(headers["Host"], "chatgpt.com")
        self.assertNotIn("Content-Length", headers)

    def test_auth_file_is_only_a_fallback(self):
        with mock.patch.object(
            codex_router, "chatgpt_auth", return_value=("file-token", "file-account")
        ):
            headers = codex_router.official_request_headers([], "chatgpt.com")
        self.assertEqual(headers["Authorization"], "Bearer file-token")
        self.assertEqual(headers["chatgpt-account-id"], "file-account")


class SecretMigrationTests(unittest.TestCase):
    def test_plaintext_key_fields_are_removed_from_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.toml.bak")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(
                    'model = "gpt-test"\n'
                    '[model_providers.VENDOR]\n'
                    'experimental_bearer_token = "secret-value"\n'
                    'name = "Vendor"\n'
                )
            self.assertTrue(setup.scrub_plaintext_keys(path))
            with open(path, encoding="utf-8") as stream:
                cleaned = stream.read()
            self.assertNotIn("secret-value", cleaned)
            self.assertNotIn("experimental_bearer_token", cleaned)
            self.assertIn('name = "Vendor"', cleaned)


if __name__ == "__main__":
    unittest.main()
