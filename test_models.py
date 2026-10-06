"""Regression checks for live discovery and migration off the static catalog."""
import copy
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import codex_router
import setup
import uninstall
from anycodex_models import merge_model_catalog


VENDORS = [{
    "name": "TEST_VENDOR", "match_prefixes": ["vendor-"],
    "models": [{
        "slug": "vendor-example", "display_name": "测试模型",
        "description": "Vendor model", "context_window": 1000000,
        "input_modalities": ["text", "image"], "default_reasoning_level": "medium",
        "supported_reasoning_levels": [{"effort": "medium", "description": "Balanced"}],
    }],
}]


def fixture_catalog():
    return {
        "models": [{
            "slug": "gpt-old", "visibility": "list", "priority": 0,
            "context_window": 200000, "base_instructions": "Official instructions",
            "model_messages": {"instructions_template": "Official template"},
            "upgrade": {"model": "gpt-next"}, "is_default": True,
            "service_tiers": [{"id": "priority"}], "multi_agent_version": "v1",
            "input_modalities": ["text"],
            "future_schema_field": {"supported": True},
        }, {"slug": "gpt-retired", "visibility": "hide", "priority": 1}],
        "fetched_at": "2026-10-06T00:00:00Z", "client_version": "0.test",
        "identity": "test-cache-identity", "etag": 'W/"test"',
    }


class CatalogMergeTests(unittest.TestCase):
    def test_official_entries_and_cache_freshness_are_unchanged(self):
        source = fixture_catalog()
        original = copy.deepcopy(source)
        merged = merge_model_catalog(source, VENDORS)
        self.assertEqual(source, original)
        self.assertEqual(merged["models"][:2], original["models"])
        for key in ("fetched_at", "client_version", "identity", "etag"):
            self.assertEqual(merged[key], original[key])
        vendor = merged["models"][-1]
        self.assertEqual(vendor["future_schema_field"], {"supported": True})
        self.assertFalse(vendor["prefer_websockets"])
        self.assertFalse(vendor["is_default"])
        self.assertEqual(vendor["base_instructions"], "")
        self.assertIsNone(vendor["model_messages"])
        self.assertEqual(vendor["service_tiers"], [])
        self.assertIsNone(vendor["upgrade"])

    def test_new_official_models_and_retirements_follow_each_refresh(self):
        first = merge_model_catalog(fixture_catalog(), VENDORS)
        refreshed = fixture_catalog()
        refreshed["models"][0]["visibility"] = "hide"
        refreshed["models"].append({"slug": "gpt-new", "visibility": "list", "priority": 2})
        second = merge_model_catalog(refreshed, VENDORS)
        self.assertNotIn("gpt-new", [m["slug"] for m in first["models"]])
        self.assertEqual(second["models"][:3], refreshed["models"])
        self.assertEqual(second["models"][-1]["slug"], "vendor-example")

    def test_idempotence_and_vendor_metadata_changes(self):
        first = merge_model_catalog(fixture_catalog(), VENDORS)
        self.assertEqual(merge_model_catalog(first, VENDORS), first)
        changed = copy.deepcopy(VENDORS)
        changed[0]["models"][0].update(context_window=500000, default_reasoning_level="high")
        changed[0]["models"][0]["supported_reasoning_levels"] = [{"effort": "high"}]
        second = merge_model_catalog(first, changed)
        self.assertEqual(len(second["models"]), len(first["models"]))
        self.assertEqual(second["models"][-1]["max_context_window"], 500000)
        self.assertEqual(second["models"][-1]["default_reasoning_level"], "high")

    def test_cache_refresh_and_repeat_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "models_cache.json"
            original = fixture_catalog()
            cache.write_text(json.dumps(original), encoding="utf-8")
            with mock.patch.object(codex_router, "CACHE_PATH", str(cache)), \
                    mock.patch.object(codex_router, "ACTIVE_VENDORS", VENDORS), \
                    mock.patch.object(codex_router, "log"):
                self.assertTrue(codex_router.inject_models_cache())
                self.assertFalse(codex_router.inject_models_cache())
                original["models"].append({"slug": "gpt-new", "visibility": "list"})
                cache.write_text(json.dumps(original), encoding="utf-8")
                self.assertTrue(codex_router.inject_models_cache())
                merged = json.loads(cache.read_text(encoding="utf-8"))
                self.assertEqual(merged["models"][:-1], original["models"])

    def test_background_injection_does_not_replace_a_concurrent_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "models_cache.json"
            original = fixture_catalog()
            refreshed = fixture_catalog()
            refreshed["models"].append({"slug": "gpt-new", "visibility": "list"})
            cache.write_text(json.dumps(original), encoding="utf-8")

            def merge_then_refresh(data, vendors):
                merged = merge_model_catalog(data, vendors)
                cache.write_text(json.dumps(refreshed), encoding="utf-8")
                return merged

            with mock.patch.object(codex_router, "CACHE_PATH", str(cache)), \
                    mock.patch.object(codex_router, "ACTIVE_VENDORS", VENDORS), \
                    mock.patch.object(codex_router, "log"), \
                    mock.patch.object(codex_router, "merge_model_catalog", side_effect=merge_then_refresh):
                self.assertFalse(codex_router.inject_models_cache())
            self.assertEqual(json.loads(cache.read_text(encoding="utf-8")), refreshed)
            self.assertFalse(Path(str(cache) + ".tmp").exists())


class ConfigMigrationTests(unittest.TestCase):
    def test_install_upgrade_and_uninstall_keep_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            legacy = Path(directory) / "models.json"
            config.write_text(
                'model = "gpt-selected"\n'
                'model_provider = "ROUTER"\n'
                f'model_catalog_json = {json.dumps(str(legacy))}\n'
                '[plugins.example]\nenabled = true\n'
                '[model_providers.ROUTER]\n# managed by anycodex\nname = "Old"\n',
                encoding="utf-8",
            )
            with mock.patch.object(setup, "CONFIG_PATH", str(config)), \
                    mock.patch.object(setup, "CODEX_HOME", directory):
                setup.update_config_toml([])
                # Reinstall must not recreate a pinned catalog or duplicate TOML.
                setup.update_config_toml([])
            cfg = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertNotIn("model_catalog_json", cfg)
            self.assertEqual(cfg["model"], "gpt-selected")
            self.assertTrue(cfg["plugins"]["example"]["enabled"])
            self.assertTrue(cfg["model_providers"]["ROUTER"]["requires_openai_auth"])
            with mock.patch.object(uninstall, "CONFIG_PATH", str(config)), \
                    mock.patch.object(uninstall, "CODEX_HOME", directory):
                uninstall.clean_config()
            cfg = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertNotIn("model_provider", cfg)
            self.assertNotIn("ROUTER", cfg.get("model_providers", {}))
            self.assertEqual(cfg["model"], "gpt-selected")
            self.assertTrue(cfg["plugins"]["example"]["enabled"])

    def test_unrelated_catalog_is_not_silently_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            text = 'model_catalog_json = "company-models.json"\n[plugins.example]\nenabled = true\n'
            config.write_text(text, encoding="utf-8")
            with mock.patch.object(setup, "CONFIG_PATH", str(config)), \
                    mock.patch.object(setup, "CODEX_HOME", directory):
                with self.assertRaises(SystemExit):
                    setup.update_config_toml([])
            self.assertEqual(config.read_text(encoding="utf-8"), text)
            with mock.patch.object(uninstall, "CONFIG_PATH", str(config)), \
                    mock.patch.object(uninstall, "CODEX_HOME", directory):
                uninstall.clean_config()
            cfg = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(cfg["model_catalog_json"], "company-models.json")

    def test_upgrade_stops_old_router_and_installs_merge_module(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "codex_router.py").write_text("# old version", encoding="utf-8")
            with mock.patch.object(setup, "CODEX_HOME", directory), \
                    mock.patch.object(uninstall, "stop_router") as stop:
                setup.install_router_files()
            stop.assert_called_once_with()
            self.assertTrue((Path(directory) / "anycodex_models.py").is_file())
            self.assertIn("_models_response", (Path(directory) / "codex_router.py").read_text(encoding="utf-8"))

    def test_stop_router_matches_exact_listener_ports(self):
        output = (
            "TCP 127.0.0.1:8232 0.0.0.0:0 LISTENING 222\n"
            "TCP 127.0.0.1:8231 0.0.0.0:0 LISTENING 111\n"
            "TCP 127.0.0.1:82310 0.0.0.0:0 LISTENING 999\n"
            "TCP 127.0.0.1:9000 127.0.0.1:8231 ESTABLISHED 888\n"
        ).encode()
        with mock.patch.object(uninstall, "IS_WIN", True), \
                mock.patch.object(uninstall.subprocess, "run", return_value=mock.Mock(stdout=output)) as run:
            uninstall.stop_router()
        kills = [call.args[0] for call in run.call_args_list if call.args[0][0] == "taskkill"]
        self.assertEqual(kills, [["taskkill", "/F", "/PID", "222"], ["taskkill", "/F", "/PID", "111"]])


class DiscoveryServer:
    """A local official-list stub plus the real AnyCodex HTTP handler."""
    def __init__(self, catalog):
        self.catalog = catalog
        self.status = 200
        self.requests = []

    def __enter__(self):
        fixture = self

        class Upstream(codex_router.http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fixture.requests.append((self.path, dict(self.headers)))
                payload = json.dumps(fixture.catalog).encode("utf-8")
                self.send_response(fixture.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("ETag", 'W/"upstream"')
                self.end_headers()
                self.wfile.write(payload)

        self.upstream = codex_router.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.router = codex_router.http.server.ThreadingHTTPServer(("127.0.0.1", 0), codex_router.Handler)
        self.patches = [
            mock.patch.object(codex_router, "tls_connection", side_effect=lambda *a: http.client.HTTPConnection(
                "127.0.0.1", self.upstream.server_port, timeout=5)),
            mock.patch.object(codex_router, "ACTIVE_VENDORS", VENDORS),
            mock.patch.object(codex_router, "chatgpt_auth", return_value=(None, None)),
            mock.patch.object(codex_router, "log"),
        ]
        for patch in self.patches:
            patch.start()
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True)
                        for s in (self.upstream, self.router)]
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *args):
        for server, thread in zip((self.router, self.upstream), reversed(self.threads)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        for patch in reversed(self.patches):
            patch.stop()

    def get(self, path="/models?client_version=test", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.router.server_port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()


class DiscoveryHTTPTests(unittest.TestCase):
    def test_live_refresh_merges_before_client_reads_it_and_keeps_auth(self):
        with DiscoveryServer(fixture_catalog()) as servers:
            status, headers, payload = servers.get(headers={
                "If-None-Match": 'W/"previous"', "If-Modified-Since": "old",
                "Authorization": "Bearer test-engine-token", "chatgpt-account-id": "test-account",
            })
            self.assertEqual(status, 200)
            self.assertNotIn("ETag", headers)
            self.assertEqual(int(headers["Content-Length"]), len(payload))
            models = json.loads(payload)["models"]
            self.assertEqual(models[-1]["slug"], "vendor-example")
            path, incoming = servers.requests[-1]
            self.assertTrue(path.endswith("/models?client_version=test"))
            self.assertNotIn("If-None-Match", incoming)
            self.assertNotIn("If-Modified-Since", incoming)
            self.assertEqual(incoming["Authorization"], "Bearer test-engine-token")
            self.assertEqual(incoming["chatgpt-account-id"], "test-account")
            servers.catalog["models"].append({"slug": "gpt-new", "visibility": "list"})
            _, _, payload = servers.get()
            self.assertIn("gpt-new", [m["slug"] for m in json.loads(payload)["models"]])

    def test_invalid_schema_and_upstream_error_pass_through(self):
        with DiscoveryServer({"unknown": ["schema"]}) as servers:
            status, _, payload = servers.get()
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload), servers.catalog)
            servers.status = 429
            servers.catalog = {"error": {"message": "Usage limited"}}
            status, _, payload = servers.get()
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(payload), servers.catalog)


@unittest.skipUnless(os.environ.get("ANYCODEX_TEST_CODEX_BINARY"), "optional real Codex binary check")
class CodexBinaryDiscoveryTests(unittest.TestCase):
    def test_real_client_sees_vendors_and_new_official_models_without_catalog_override(self):
        binary = os.environ["ANYCODEX_TEST_CODEX_BINARY"]
        source = Path(codex_router.CODEX_HOME)
        catalog = json.loads((source / "models_cache.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="anycodex-model-check-") as directory, \
                DiscoveryServer(catalog) as servers:
            root = Path(directory)
            if (source / "auth.json").exists():
                shutil.copy2(source / "auth.json", root / "auth.json")
            config = root / "config.toml"
            config.write_text(
                'model_provider = "ROUTER"\n'
                '[model_providers.ROUTER]\nname = "AnyCodex test"\n'
                f'base_url = "http://127.0.0.1:{servers.router.server_port}"\n'
                'requires_openai_auth = true\nwire_api = "responses"\n', encoding="utf-8")
            self.assertNotIn("model_catalog_json", tomllib.loads(config.read_text(encoding="utf-8")))
            env = dict(os.environ, CODEX_HOME=directory)
            for refresh in (False, True):
                if refresh:
                    new = copy.deepcopy(catalog["models"][0])
                    new.update(slug="gpt-future-regression", display_name="Future regression model",
                               visibility="list", is_default=False)
                    servers.catalog["models"].append(new)
                    # Model discovery normally reuses a fresh cache. Expire it
                    # to exercise the next genuine official-list refresh.
                    cached_path = root / "models_cache.json"
                    cached = json.loads(cached_path.read_text(encoding="utf-8"))
                    cached["fetched_at"] = "2000-01-01T00:00:00Z"
                    cached_path.write_text(json.dumps(cached), encoding="utf-8")
                result = subprocess.run([binary, "debug", "models"], env=env, cwd=directory,
                                        capture_output=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                models = json.loads(result.stdout)["models"]
                visible = {m["slug"] for m in models if m["visibility"] == "list"}
                self.assertIn("gpt-6.1-sol", visible)
                self.assertIn("vendor-example", visible)
                if refresh:
                    self.assertIn("gpt-future-regression", visible)
                self.assertEqual(len([m for m in models if m["slug"] == "vendor-example"]), 1)
            self.assertGreaterEqual(len(servers.requests), 2)


if __name__ == "__main__":
    unittest.main()
