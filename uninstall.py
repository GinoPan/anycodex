#!/usr/bin/env python3
"""AnyCodex uninstaller.

Removes the router autostart entry, stops the router, strips every
anycodex-managed block from ~/.codex/config.toml (restoring the official
provider), and deletes the installed router/catalog files. Backups created by
setup.py are kept.
"""
import os
import platform
import subprocess
import sys

from anycodex_common import (CODEX_HOME, drop_top_level_keys, strip_managed_sections,
                             remove_legacy_catalog_override)
from anycodex_credentials import delete_credential

CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
IS_WIN = platform.system() == "Windows"


def stop_router():
    import json
    port = 8231
    supervisor_port = 8232
    cfg_path = os.path.join(CODEX_HOME, "router_config.json")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "router_config.json")
    try:
        with open(cfg_path, encoding="utf-8") as stream:
            installed = json.load(stream)
        port = installed.get("port", 8231)
        supervisor_port = installed.get("supervisor_port", port + 1)
    except Exception:
        pass
    if IS_WIN:
        r = subprocess.run(["netstat", "-ano"], capture_output=True)
        # netstat output uses the OEM codepage (GBK on zh-CN); decode leniently
        text = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
        # Stop the supervisor first, otherwise it immediately respawns router.
        for target_port in (supervisor_port, port):
            pids = set()
            for line in text.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0] == "TCP"
                        and parts[1].rsplit(":", 1)[-1] == str(target_port)
                        and parts[3] == "LISTENING"):
                    pids.add(parts[-1])
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                print("stopped process pid", pid, "on port", target_port)
            if pids:
                r = subprocess.run(["netstat", "-ano"], capture_output=True)
                text = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
    else:
        subprocess.run(["pkill", "-f", "codex_router.py"], capture_output=True)
        print("pkill codex_router.py sent")


def remove_autostart():
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "AnyCodexRouter")
            print("removed HKCU Run entry AnyCodexRouter")
        except FileNotFoundError:
            pass
        vbs = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup", "anycodex-router.vbs")
        shortcut = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                                "Start Menu", "Programs", "Startup",
                                "AnyCodex Router.lnk")
        for path in (vbs, shortcut):
            if os.path.exists(path):
                os.remove(path)
                print("removed", path)
    else:
        print("macOS/Linux: remove any LaunchAgent/cron entry you created for codex_router.py")


def clean_config():
    import json
    known = {"model_providers.ROUTER"}
    prefixes = ()
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "router_config.json"), encoding="utf-8") as stream:
            cfg_repo = json.load(stream)
        known |= {"model_providers.%s" % v["key_config_section"] for v in cfg_repo.get("vendors", [])}
        prefixes = tuple(p for v in cfg_repo.get("vendors", []) for p in v.get("match_prefixes", []))
    except Exception:
        pass
    with open(CONFIG_PATH, encoding="utf-8") as stream:
        text = stream.read()
    text = remove_legacy_catalog_override(text, CODEX_HOME)
    cleaned = drop_top_level_keys(strip_managed_sections(text, known),
                                  ("model_provider",))
    import tomllib
    cfg = tomllib.loads(cleaned)
    with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(cleaned)
    print("config.toml restored (anycodex blocks removed, TOML valid)."
          if "ROUTER" not in cfg.get("model_providers", {})
          else "WARNING: ROUTER block still present, remove manually.")
    model = cfg.get("model")
    if model and prefixes and str(model).startswith(prefixes):
        print("NOTE: default model is %r (a third-party slug). Pick an official model "
              "in the app, or edit config.toml." % model)


def main():
    print("=== AnyCodex uninstall ===")
    with open(CONFIG_PATH, encoding="utf-8") as stream:
        previous = stream.read()
    legacy_catalog = remove_legacy_catalog_override(previous, CODEX_HOME) != previous
    stop_router()
    remove_autostart()
    clean_config()
    try:
        import json
        cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "router_config.json"), encoding="utf-8"))
        for vendor in cfg.get("vendors", []):
            if delete_credential(vendor["key_config_section"]):
                print("removed Windows credential", vendor["key_config_section"])
    except Exception as e:
        print("WARNING: credential cleanup failed:", e)
    files = ("codex_router.py", "router_config.json", "router.log",
              "router-supervisor.log", "anycodex_supervisor.py", "anycodex_credentials.py",
              "anycodex_models.py",
              "anycodex_profile_state.json")
    if legacy_catalog:
        files += ("models.json",)
    for f in files:
        p = os.path.join(CODEX_HOME, f)
        if os.path.exists(p):
            os.remove(p)
            print("removed", p)
    print("""
Done. Restart the ChatGPT desktop app to go back to pure official models.
Config backups (config.toml.bak-anycodex-*) were kept in %s.
""" % CODEX_HOME)


if __name__ == "__main__":
    main()
