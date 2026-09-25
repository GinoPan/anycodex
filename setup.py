#!/usr/bin/env python3
"""AnyCodex interactive installer.

Configures the ChatGPT desktop app (Codex engine) to route model requests
through the local AnyCodex router, so official OpenAI models and third-party
models from any vendor that speaks the OpenAI Responses protocol (GLM and
DeepSeek are preset) can be mixed in the same model picker.

What it does:
  1. backs up ~/.codex/config.toml (and models.json if present)
  2. adds a local ROUTER provider + vendor key blocks to config.toml
  3. generates ~/.codex/models.json (model catalog for the picker/engine)
  4. installs the router into ~/.codex and starts it
  5. registers an autostart entry (Windows; other systems print manual steps)

Blocks written here carry a marker comment; uninstall.py and repeated runs of
this script remove previous marked blocks cleanly before writing new ones.

Run:  python setup.py
"""
import getpass
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time

from anycodex_common import CODEX_HOME, MARKER

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTER_CONFIG = json.load(open(os.path.join(REPO_DIR, "router_config.json"), encoding="utf-8"))
from anycodex_common import drop_top_level_keys, strip_managed_sections  # noqa: E402

IS_WIN = platform.system() == "Windows"
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

TOP_KEYS = ("model_provider", "model_catalog_json")


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
        print("  [%d] %s - %s (models: %s)" % (i, v["name"], v.get("label", ""),
                                                ", ".join(m["slug"] for m in v["models"])))
    raw = input("Enable which? (comma numbers, Enter = all): ").strip()
    if not raw:
        return ROUTER_CONFIG["vendors"]
    idx = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    return [ROUTER_CONFIG["vendors"][i] for i in idx]


def collect_keys(vendors):
    print("\nAPI keys are stored in %s ([model_providers.<name>] blocks)." % CONFIG_PATH)
    missing = []
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
            missing.append(v["name"])
            print("  no key for %s - it will be skipped (its models stay unrouted)" % v["name"])
    return [v for v in vendors if v["name"] not in missing]


def backup(path, tag):
    if os.path.exists(path):
        bak = "%s.bak-anycodex-%s" % (path, tag)
        shutil.copy2(path, bak)
        print("backed up %s -> %s" % (os.path.basename(path), os.path.basename(bak)))


def update_config_toml(vendors):
    src = open(CONFIG_PATH, encoding="utf-8").read()
    known = {"model_providers.ROUTER"} | {"model_providers.%s" % v["key_config_section"]
                                          for v in ROUTER_CONFIG["vendors"]}
    text = strip_managed_sections(src, known)        # remove previous anycodex/vendor blocks
    text = drop_top_level_keys(text, TOP_KEYS)       # and previous top-level keys

    lines = text.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))
    insert = "".join('%s = "%s"\n' % (k, v) for k, v in
                     (("model_provider", "ROUTER"), ("model_catalog_json", "~/.codex/models.json")))
    text = "".join(lines[:first_table]).rstrip("\n") + "\n" + insert + "".join(lines[first_table:])

    text += (
        "\n[model_providers.ROUTER]\n%s\n"
        'name = "AnyCodex Local Router"\n'
        'base_url = "http://127.0.0.1:%d"\n'
        'experimental_bearer_token = "anycodex-local"\n'
        'wire_api = "responses"\n' % (MARKER, ROUTER_CONFIG["port"])
    )
    for v in vendors:
        key = os.environ.get(v["key_env"], "")
        text += (
            "\n[model_providers.%s]\n%s\n"
            'name = "%s"\n'
            'base_url = "https://%s"\n'
            'experimental_bearer_token = "%s"\n'
            'wire_api = "responses"\n' % (v["key_config_section"], MARKER, v["name"], v["host"], key)
        )
    open(CONFIG_PATH, "w", encoding="utf-8", newline="\n").write(text)
    import tomllib
    cfg = tomllib.load(open(CONFIG_PATH, "rb"))  # validate before we call it done
    assert cfg.get("model_provider") == "ROUTER"
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
    # official models from the server list keep their own visibility: listed
    # entries show in the picker even when the account is rate-limited (the
    # server-side list channel fails then), hidden ones stay hidden
    try:
        cache = json.load(open(CACHE_PATH, encoding="utf-8"))
        have = {m["slug"] for m in models}
        for m in cache.get("models", []):
            if m["slug"] not in have:
                models.append(dict(m))
    except Exception:
        print("NOTE: models_cache.json not readable, official entries skipped (harmless).")
    json.dump({"models": models}, open(CATALOG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("model catalog written ->", CATALOG_PATH, "(%d entries)" % len(models))


def install_router_files():
    for f in ("codex_router.py", "router_config.json"):
        shutil.copy2(os.path.join(REPO_DIR, f), os.path.join(CODEX_HOME, f))
    print("router files installed to", CODEX_HOME)


def start_router():
    port = ROUTER_CONFIG["port"]
    s = socket.socket()
    s.settimeout(2)
    busy = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    if busy:
        print("NOTE: port %d already in use - the router was NOT started here." % port)
        print("      If an older anycodex/router instance is running, stop it (uninstall.py or")
        print("      taskkill) and rerun setup; if it IS this router, nothing to do.")
        return
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe") if IS_WIN else sys.executable
    if not os.path.exists(py):
        py = sys.executable
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([py, os.path.join(CODEX_HOME, "codex_router.py")], creationflags=flags)
    time.sleep(2)
    s = socket.socket()
    s.settimeout(3)
    ok = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    print("router started, port %d listening: %s" % (port, ok))


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


def main():
    print("=== AnyCodex setup ===")
    preflight()
    vendors = choose_vendors()
    vendors = collect_keys(vendors)
    if not vendors:
        die("no vendor with a key selected - nothing to do.")
    tag = time.strftime("%Y%m%d-%H%M%S")
    backup(CONFIG_PATH, tag)
    backup(CATALOG_PATH, tag)
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
