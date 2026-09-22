#!/usr/bin/env python3
"""AnyCodex uninstaller.

Removes the router autostart entry, stops the router, strips the AnyCodex
blocks from ~/.codex/config.toml (restoring the official provider), and
deletes the installed router/catalog files. Your config backup files are kept.
"""
import json
import os
import platform
import subprocess
import sys

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".codex")
CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
IS_WIN = platform.system() == "Windows"


def stop_router():
    if IS_WIN:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
        pids = {ln.split()[-1] for ln in r.stdout.splitlines()
                if ":8231" in ln and "LISTENING" in ln}
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
    src = open(CONFIG_PATH, encoding="utf-8").read()
    lines = src.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    drop_top = {"model_provider", "model_catalog_json"}
    out = [l for i, l in enumerate(lines)
           if not (i < first_table and any(l.strip().startswith(k) for k in drop_top))]
    text = "".join(l for l in out if not l.lstrip().startswith("[model_providers.ROUTER]"))
    # drop vendor key blocks (sections written by setup.py are all-caps names)
    import re
    text = re.sub(r"\n?\[model_providers\.(ZAI|DEEPSEEK)\]\n(name = \"[^\"]*\"\nbase_url = \"[^\"]*\"\n"
                  r"experimental_bearer_token = \"[^\"]*\"\nwire_api = \"responses\"\n?)", "\n", text)
    open(CONFIG_PATH, "w", encoding="utf-8", newline="\n").write(text)
    import tomllib
    cfg = tomllib.load(open(CONFIG_PATH, "rb"))
    if "ROUTER" in cfg.get("model_providers", {}):
        print("WARNING: ROUTER block still present, remove manually.")
    else:
        print("config.toml restored to official provider.")
    if cfg.get("model") and str(cfg["model"]).startswith(("glm", "deepseek")):
        print("NOTE: default model is %r (a third-party slug). Switch the model in the "
              "app picker or edit config.toml to an official model." % cfg["model"])


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
