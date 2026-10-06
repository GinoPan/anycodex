#!/usr/bin/env python3
"""AnyCodex interactive installer.

Configures the ChatGPT desktop app (Codex engine) to route model requests
through the local AnyCodex router, so official OpenAI models and third-party
models from any vendor that speaks the OpenAI Responses protocol (GLM and
DeepSeek are preset) can be mixed in the same model picker.

What it does:
  1. backs up ~/.codex/config.toml (and models.json if present)
  2. stores vendor keys in Windows Credential Manager and adds ROUTER to config.toml
  3. removes the legacy static catalog override (official discovery stays live)
  4. installs the router into ~/.codex and starts it
  5. registers an autostart entry (Windows; other systems print manual steps)

Blocks written here carry a marker comment; uninstall.py and repeated runs of
this script remove previous marked blocks cleanly before writing new ones.

Run:  python setup.py
"""
import getpass
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request

from anycodex_common import CODEX_HOME, MARKER, remove_legacy_catalog_override
from anycodex_credentials import read_credential, write_credential

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTER_CONFIG = json.load(open(os.path.join(REPO_DIR, "router_config.json"), encoding="utf-8"))
from anycodex_common import drop_top_level_keys, strip_managed_sections  # noqa: E402

IS_WIN = platform.system() == "Windows"
CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
CATALOG_PATH = os.path.join(CODEX_HOME, "models.json")
TOP_KEYS = ("model_provider",)


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
    import tomllib
    with open(CONFIG_PATH, encoding="utf-8") as stream:
        current = remove_legacy_catalog_override(stream.read(), CODEX_HOME)
    if tomllib.loads(current).get("model_catalog_json"):
        die("an unrelated model_catalog_json is configured. Remove that override "
            "before installing AnyCodex; it prevents live model discovery.")
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
    print("\nAPI keys are stored in Windows Credential Manager (never in config.toml)."
          if IS_WIN else "\nAPI keys are read from the configured environment variables.")
    try:
        import tomllib
        legacy_cfg = tomllib.load(open(CONFIG_PATH, "rb"))
    except Exception:
        legacy_cfg = {}
    missing = []
    for v in vendors:
        env = v["key_env"]
        section = v["key_config_section"]
        current = read_credential(section) if IS_WIN else None
        source = "Windows Credential Manager" if current else None
        if not current:
            current = os.environ.get(env)
            source = env if current else None
        if not current:
            current = (((legacy_cfg.get("model_providers") or {}).get(section) or {})
                       .get("experimental_bearer_token"))
            source = "legacy config.toml" if current else None
        if current:
            use = input("%s: found an existing key in %s. Use it? [Y/n]: "
                        % (v["name"], source)).strip().lower()
            if use in ("", "y", "yes"):
                if IS_WIN:
                    write_credential(section, current)
                else:
                    os.environ[env] = current
                continue
        key = getpass.getpass("%s: paste API key (%s): " % (v["name"], v.get("label", ""))).strip()
        if key:
            if IS_WIN:
                write_credential(section, key)
            else:
                os.environ[env] = key
        else:
            missing.append(v["name"])
            print("  no key for %s - it will be skipped (its models stay unrouted)" % v["name"])
    return [v for v in vendors if v["name"] not in missing]


def backup(path, tag):
    if os.path.exists(path):
        bak = "%s.bak-anycodex-%s" % (path, tag)
        shutil.copy2(path, bak)
        if os.path.abspath(path) == os.path.abspath(CONFIG_PATH):
            scrub_plaintext_keys(bak)
        print("backed up %s -> %s" % (os.path.basename(path), os.path.basename(bak)))


def scrub_plaintext_keys(path):
    """Remove legacy API-key assignments while keeping the TOML backup usable."""
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    cleaned = re.sub(
        r"(?m)^[ \t]*(?:experimental_bearer_token|api_key)\s*=.*(?:\r?\n|$)",
        "",
        text,
    )
    if cleaned != text:
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(cleaned)
        return True
    return False


def scrub_existing_config_backups():
    changed = 0
    for path in glob.glob(CONFIG_PATH + "*"):
        if os.path.isfile(path) and scrub_plaintext_keys(path):
            changed += 1
    if changed:
        print("removed plaintext API-key fields from %d config file(s)" % changed)


def update_config_toml(vendors):
    with open(CONFIG_PATH, encoding="utf-8") as stream:
        src = stream.read()
    src = remove_legacy_catalog_override(src, CODEX_HOME)
    import tomllib
    if tomllib.loads(src).get("model_catalog_json"):
        die("unrelated model_catalog_json override found; config.toml left unchanged.")
    known = {"model_providers.ROUTER"} | {"model_providers.%s" % v["key_config_section"]
                                          for v in ROUTER_CONFIG["vendors"]}
    text = strip_managed_sections(src, known)        # remove previous anycodex/vendor blocks
    text = drop_top_level_keys(text, TOP_KEYS)       # and previous top-level keys

    lines = text.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))
    insert = 'model_provider = "ROUTER"\n'
    text = "".join(lines[:first_table]).rstrip("\n") + "\n" + insert + "".join(lines[first_table:])

    text += (
        "\n[model_providers.ROUTER]\n%s\n"
        'name = "AnyCodex Local Router"\n'
        'base_url = "http://127.0.0.1:%d"\n'
        'requires_openai_auth = true\n'
        'wire_api = "responses"\n' % (MARKER, ROUTER_CONFIG["port"])
    )
    cfg = tomllib.loads(text)  # validate before replacing the user's config
    assert cfg.get("model_provider") == "ROUTER"
    assert "model_catalog_json" not in cfg
    with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    print("config.toml updated and validated (model_provider=ROUTER, no plaintext vendor keys)")


def install_router_files():
    # A healthy old process would otherwise keep its imported code and never
    # pick up the new /models merge, even after the files were replaced.
    if os.path.exists(os.path.join(CODEX_HOME, "codex_router.py")):
        from uninstall import stop_router
        stop_router()
    for f in ("codex_router.py", "router_config.json", "anycodex_credentials.py",
              "anycodex_models.py", "anycodex_supervisor.py"):
        shutil.copy2(os.path.join(REPO_DIR, f), os.path.join(CODEX_HOME, f))
    print("router files installed to", CODEX_HOME)


def start_router():
    port = ROUTER_CONFIG["port"]
    supervisor_port = ROUTER_CONFIG.get("supervisor_port", port + 1)
    health = "http://127.0.0.1:%d/healthz" % port

    def healthy():
        try:
            with urllib.request.urlopen(health, timeout=1) as response:
                return (response.status == 200
                        and json.load(response).get("service") == "anycodex-router")
        except Exception:
            return False

    def supervisor_running():
        import socket
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", supervisor_port)) == 0

    if healthy() and supervisor_running():
        print("router and supervisor already healthy on ports", port, "and", supervisor_port)
        return
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe") if IS_WIN else sys.executable
    if not os.path.exists(py):
        py = sys.executable
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [py, os.path.join(CODEX_HOME, "anycodex_supervisor.py")],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, creationflags=flags,
    )
    for _ in range(20):
        if healthy() and supervisor_running():
            print("router supervisor started; health check passed on port", port)
            return
        time.sleep(0.5)
    die("router supervisor started but /healthz did not become ready")


def register_autostart():
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe") if IS_WIN else sys.executable
    supervisor = os.path.join(CODEX_HOME, "anycodex_supervisor.py")
    if IS_WIN:
        # Some endpoint-security products block script-valued HKCU Run entries.
        # A normal Shell Link in the user's Startup folder is both visible and
        # does not require administrator privileges.
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

            @classmethod
            def parse(cls, value):
                parsed = uuid.UUID(value)
                fields = parsed.fields
                tail = bytes((fields[3], fields[4])) + fields[5].to_bytes(6, "big")
                return cls(fields[0], fields[1], fields[2],
                           (ctypes.c_ubyte * 8).from_buffer_copy(tail))

        def check_hresult(result, operation):
            if result < 0:
                raise OSError("%s failed (HRESULT 0x%08X)" %
                              (operation, result & 0xFFFFFFFF))

        def method(pointer, index, result_type, *argument_types):
            table = ctypes.cast(pointer, ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p))).contents
            return ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p,
                                      *argument_types)(table[index])

        startup = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                               "Start Menu", "Programs", "Startup")
        os.makedirs(startup, exist_ok=True)
        shortcut = os.path.join(startup, "AnyCodex Router.lnk")
        shell_link = ctypes.c_void_p()
        persist_file = ctypes.c_void_p()
        clsid_shell_link = GUID.parse("00021401-0000-0000-C000-000000000046")
        iid_shell_link = GUID.parse("000214F9-0000-0000-C000-000000000046")
        iid_persist_file = GUID.parse("0000010B-0000-0000-C000-000000000046")
        ole32 = ctypes.OleDLL("ole32")
        ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(GUID),
                                           ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.HRESULT
        initialized = ole32.CoInitialize(None) >= 0
        try:
            result = ole32.CoCreateInstance(
                ctypes.byref(clsid_shell_link), None, 1,
                ctypes.byref(iid_shell_link), ctypes.byref(shell_link))
            check_hresult(result, "create Startup shortcut")
            check_hresult(method(shell_link, 20, ctypes.HRESULT, wintypes.LPCWSTR)(
                shell_link, py), "set shortcut target")
            check_hresult(method(shell_link, 11, ctypes.HRESULT, wintypes.LPCWSTR)(
                shell_link, '"%s"' % supervisor), "set shortcut arguments")
            check_hresult(method(shell_link, 9, ctypes.HRESULT, wintypes.LPCWSTR)(
                shell_link, CODEX_HOME), "set shortcut working directory")
            check_hresult(method(shell_link, 7, ctypes.HRESULT, wintypes.LPCWSTR)(
                shell_link, "Keep the AnyCodex local router running"),
                "set shortcut description")
            check_hresult(method(shell_link, 0, ctypes.HRESULT,
                                 ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                shell_link, ctypes.byref(iid_persist_file), ctypes.byref(persist_file)),
                "open shortcut file writer")
            check_hresult(method(persist_file, 6, ctypes.HRESULT,
                                 wintypes.LPCWSTR, wintypes.BOOL)(
                persist_file, shortcut, True), "save Startup shortcut")
        finally:
            if persist_file:
                method(persist_file, 2, wintypes.ULONG)(persist_file)
            if shell_link:
                method(shell_link, 2, wintypes.ULONG)(shell_link)
            if initialized:
                ole32.CoUninitialize()

        # Remove legacy startup methods left by older AnyCodex releases.
        startup = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                               "Start Menu", "Programs", "Startup")
        vbs = os.path.join(startup, "anycodex-router.vbs")
        if os.path.exists(vbs):
            os.remove(vbs)
        print("autostart registered ->", shortcut)
    else:
        print("macOS/Linux: register autostart manually, e.g. a LaunchAgent running:\n"
              "  %s %s\n" % (py, supervisor))


def main():
    print("=== AnyCodex setup ===")
    preflight()
    vendors = choose_vendors()
    vendors = collect_keys(vendors)
    if not vendors:
        die("no vendor with a key selected - nothing to do.")
    scrub_existing_config_backups()
    tag = time.strftime("%Y%m%d-%H%M%S")
    backup(CONFIG_PATH, tag)
    backup(CATALOG_PATH, tag)
    update_config_toml(vendors)
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
