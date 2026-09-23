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

from anycodex_common import CODEX_HOME, drop_top_level_keys, strip_managed_sections

CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
IS_WIN = platform.system() == "Windows"


def stop_router():
    import json
    port = 8231
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "router_config.json")
    try:
        port = json.load(open(cfg_path, encoding="utf-8")).get("port", 8231)
    except Exception:
        pass
    if IS_WIN:
        r = subprocess.run(["netstat", "-ano"], capture_output=True)
        # netstat output uses the OEM codepage (GBK on zh-CN); decode leniently
        text = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
        pids = {ln.split()[-1] for ln in text.splitlines()
                if ":%d" % port in ln and "LISTENING" in ln}
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            print("stopped router pid", pid)
    else:
        subprocess.run(["pkill", "-f", "codex_router.py"], capture_output=True)
        print("pkill codex_router.py sent")


def remove_autostart():
    if IS_WIN:
        vbs = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup", "anycodex-router.vbs")
        if os.path.exists(vbs):
            os.remove(vbs)
            print("removed", vbs)
    else:
        print("macOS/Linux: remove any LaunchAgent/cron entry you created for codex_router.py")


def clean_config():
    import json
    known = {"model_providers.ROUTER"}
    prefixes = ()
    try:
        cfg_repo = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "router_config.json"), encoding="utf-8"))
        known |= {"model_providers.%s" % v["key_config_section"] for v in cfg_repo.get("vendors", [])}
        prefixes = tuple(p for v in cfg_repo.get("vendors", []) for p in v.get("match_prefixes", []))
    except Exception:
        pass
    text = open(CONFIG_PATH, encoding="utf-8").read()
    cleaned = drop_top_level_keys(strip_managed_sections(text, known),
                                  ("model_provider", "model_catalog_json"))
    open(CONFIG_PATH, "w", encoding="utf-8", newline="\n").write(cleaned)
    import tomllib
    cfg = tomllib.load(open(CONFIG_PATH, "rb"))  # validate
    print("config.toml restored (anycodex blocks removed, TOML valid)."
          if "ROUTER" not in cfg.get("model_providers", {})
          else "WARNING: ROUTER block still present, remove manually.")
    model = cfg.get("model")
    if model and prefixes and str(model).startswith(prefixes):
        print("NOTE: default model is %r (a third-party slug). Pick an official model "
              "in the app, or edit config.toml." % model)


def main():
    print("=== AnyCodex uninstall ===")
    stop_router()
    remove_autostart()
    clean_config()
    for f in ("codex_router.py", "router_config.json", "models.json", "router.log"):
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
