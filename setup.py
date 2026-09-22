#!/usr/bin/env python3
"""AnyCodex interactive installer.

Configures the ChatGPT desktop app (Codex engine) to route model requests
through the local AnyCodex router, so official OpenAI models and third-party
models (GLM, DeepSeek, ...) can be mixed in the same model picker.

What it does:
  1. backs up ~/.codex/config.toml
  2. adds a local ROUTER provider + vendor key blocks to config.toml
  3. generates ~/.codex/models.json (model catalog for the picker/engine)
  4. installs the router into ~/.codex and starts it
  5. registers an autostart entry (Windows; macOS prints manual steps)

Run:  python setup.py
"""
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTER_CONFIG = json.load(open(os.path.join(REPO_DIR, "router_config.json"), encoding="utf-8"))

IS_WIN = platform.system() == "Windows"
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".codex")
CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
CATALOG_PATH = os.path.join(CODEX_HOME, "models.json")
CACHE_PATH = os.path.join(CODEX_HOME, "models_cache.json")

CATALOG_BOILERPLATE = {
    "prefer_websockets": False,
    "shell_type": "shell_command",
    "visibility": "list",
    "supported_in_api": True,
    "base_instructions": "",
    "supports_reasoning_summaries": True,
    "default_reasoning_summary": "none",
    "support_verbosity": False,
    "apply_patch_tool_type": "freeform",
    "truncation_policy": {"mode": "bytes", "limit": 10000},
    "effective_context_window_percent": 95,
    "supports_parallel_tool_calls": True,
    "experimental_supported_tools": [],
}

TOP_KEYS = {
    "model_provider": '"ROUTER"',
    "model_catalog_json": '"~/.codex/models.json"',
}


def die(msg):
    print("ERROR:", msg)
    sys.exit(1)


def preflight():
    if sys.version_info < (3, 11):
        die("Python 3.11+ required (tomllib).")
    if not os.path.isdir(CODEX_HOME):
        die("Codex home not found at %s. Install the ChatGPT desktop app and log in first." % CODEX_HOME)
    if not os.path.exists(CONFIG_PATH):
        die("config.toml not found in %s." % CODEX_HOME)
    if not IS_WIN:
        print("NOTE: %s is not tested; the router core is pure Python but autostart differs." % platform.system())


def choose_vendors():
    print("\nVendors available in router_config.json:")
    for i, v in enumerate(ROUTER_CONFIG["vendors"]):
        print("  [%d] %s - %s (models: %s)" % (i, v["name"], v.get("label", ""), ", ".join(m["slug"] for m in v["models"])))
    raw = input("Enable which? (comma numbers, Enter = all): ").strip()
    if not raw:
        return ROUTER_CONFIG["vendors"]
    idx = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    return [ROUTER_CONFIG["vendors"][i] for i in idx]


def collect_keys(vendors):
    print("\nAPI keys are stored in %s ([model_providers.<name>] blocks)." % CONFIG_PATH)
    for v in vendors:
        env = v["key_env"]
        current = os.environ.get(env)
        if current:
            use = input("%s: found %s in environment (…%s). Use it? [Y/n]: "
                        % (v["name"], env, current[-4:])).strip().lower()
            if use in ("", "y", "yes"):
                continue
        key = getpass.getpass("%s: paste API key (%s): " % (v["name"], v.get("label", ""))).strip()
        if key:
            os.environ[env] = key
        else:
            print("  skipped %s (no key; its models will fail until a key is set)" % v["name"])


def backup_config():
    import time
    bak = CONFIG_PATH + ".bak-anycodex-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CONFIG_PATH, bak)
    print("config.toml backed up ->", bak)
    return bak


def update_config_toml(vendors):
    src = open(CONFIG_PATH, encoding="utf-8").read()
    lines = src.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))

    # top-level keys: drop old occurrences, insert fresh ones right before the first table
    top = [l for i, l in enumerate(lines[:first_table])
           if not any(l.strip().startswith(k + " ") or l.strip().startswith(k + "=") for k in TOP_KEYS)]
    top += ["%s = %s\n" % (k, v) for k, v in TOP_KEYS.items()]

    body = lines[first_table:]
    # strip previous AnyCodex blocks (idempotent reinstall)
    body = [l for l in body if not l.lstrip().startswith("[model_providers.ROUTER]")]
    text = "".join(top + body).rstrip("\n") + "\n"

    text += (
        "\n[model_providers.ROUTER]\n"
        'name = "AnyCodex Local Router"\n'
        'base_url = "http://127.0.0.1:%d"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n" % ROUTER_CONFIG["port"]
    )
    for v in vendors:
        key = os.environ.get(v["key_env"], "")
        text += (
            "\n[model_providers.%s]\n"
            'name = "%s"\n'
            'base_url = "https://%s"\n'
            'experimental_bearer_token = "%s"\n'
            'wire_api = "responses"\n' % (v["key_config_section"], v["name"], v["host"], key)
        )
    open(CONFIG_PATH, "w", encoding="utf-8", newline="\n").write(text)
    import tomllib
    tomllib.load(open(CONFIG_PATH, "rb"))  # validate
    print("config.toml updated and validated (model_provider=ROUTER, %d vendor key blocks)" % len(vendors))


def write_catalog(vendors):
    models = []
    prio = 0
    for v in vendors:
        for m in v["models"]:
            entry = dict(CATALOG_BOILERPLATE)
            entry.update(m)
            entry["provider"] = v["name"]
            entry["priority"] = prio
            entry["max_context_window"] = m["context_window"]
            models.append(entry)
            prio += 1
    # hide official models from our catalog: the picker shows them from the
    # server-provided list anyway; hidden entries just supply engine metadata
    try:
        cache = json.load(open(CACHE_PATH, encoding="utf-8"))
        have = {m["slug"] for m in models}
        for m in cache.get("models", []):
            if m["slug"] not in have:
                e = dict(m)
                e["visibility"] = "hide"
                models.append(e)
    except Exception:
        print("NOTE: models_cache.json not readable, official entries skipped (harmless).")
    json.dump({"models": models}, open(CATALOG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("model catalog written ->", CATALOG_PATH, "(%d entries)" % len(models))


def install_router_files():
    for f in ("codex_router.py", "router_config.json"):
        shutil.copy2(os.path.join(REPO_DIR, f), os.path.join(CODEX_HOME, f))
    print("router files installed to", CODEX_HOME)


def start_router():
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe") if IS_WIN else sys.executable
    if not os.path.exists(py):
        py = sys.executable
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([py, os.path.join(CODEX_HOME, "codex_router.py")], creationflags=flags)
    import socket
    time.sleep(2)
    s = socket.socket()
    s.settimeout(3)
    ok = s.connect_ex(("127.0.0.1", ROUTER_CONFIG["port"])) == 0
    s.close()
    print("router started, port %d listening: %s" % (ROUTER_CONFIG["port"], ok))


def register_autostart():
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe") if IS_WIN else sys.executable
    router = os.path.join(CODEX_HOME, "codex_router.py")
    if IS_WIN:
        startup = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                               "Start Menu", "Programs", "Startup")
        vbs = os.path.join(startup, "anycodex-router.vbs")
        open(vbs, "w", encoding="utf-8").write(
            'CreateObject("Wscript.Shell").Run """%s"" ""%s""", 0, False\n' % (py, router))
        print("autostart registered ->", vbs)
    else:
        print("macOS/Linux: register autostart manually, e.g. a LaunchAgent running:\n"
              "  %s %s\n" % (py, router))


import time  # noqa: E402  (used by backup_config)

def main():
    print("=== AnyCodex setup ===")
    preflight()
    vendors = choose_vendors()
    collect_keys(vendors)
    backup_config()
    update_config_toml(vendors)
    write_catalog(vendors)
    install_router_files()
    start_router()
    register_autostart()
    print("""
=== Done ===
1. Fully quit and restart the ChatGPT desktop app (picker loads once at startup).
2. Open a NEW conversation, open the bottom-right model picker:
   official models + your custom models should all be listed.
3. Use third-party models in conversations created from now on
   (threads pin their provider at creation; old threads keep the old routing).

Log: %s
Uninstall: python uninstall.py (in this folder)
""" % os.path.join(CODEX_HOME, "router.log"))


if __name__ == "__main__":
    main()
