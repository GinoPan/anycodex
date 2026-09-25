#!/usr/bin/env python3
"""AnyCodex profile switcher.

Switches the active Codex provider profile by rewriting model_provider
(and the default model) in ~/.codex/config.toml, then reminds you to
fully restart the ChatGPT desktop app (the engine does not hot-reload
provider changes).

Profiles
  mixed      official + third-party mixed in one picker via the local
             router (the default AnyCodex experience; requires official
             quota to be alive - see README known issues)
  glm        third-party only: model_provider points directly at the
             GLM vendor block. Bypasses the desktop app's account
             rate-limit composer lockout.
  deepseek   third-party only, pointing at the DeepSeek vendor block.
  official   pure official OpenAI configuration (anycodex keys removed).

Usage:  python profile.py [mixed|glm|deepseek|official]
"""
import json
import os
import shutil
import sys
import time

from anycodex_common import CODEX_HOME, MARKER, drop_top_level_keys, strip_managed_sections

CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
STATE_PATH = os.path.join(CODEX_HOME, "anycodex_profile_state.json")
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_MODEL = {"mixed": "gpt-6-astra", "glm": "glm-5.3", "deepseek": "deepseek-flash", "official": "gpt-6-astra"}
PROFILE_PROVIDER = {"mixed": "ROUTER", "glm": None, "deepseek": None, "official": None}  # None resolved below


def load_vendors():
    cfg = json.load(open(os.path.join(REPO_DIR, "router_config.json"), encoding="utf-8"))
    return {v["key_config_section"]: v for v in cfg["vendors"]}, cfg


def resolve_providers():
    vendors, _ = load_vendors()
    glm = next((s for s, v in vendors.items() if any(p in ("glm",) for p in v.get("match_prefixes", []))), None)
    ds = next((s for s, v in vendors.items() if any(p in ("deepseek",) for p in v.get("match_prefixes", []))), None)
    PROFILE_PROVIDER["glm"] = glm
    PROFILE_PROVIDER["deepseek"] = ds


def read_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def write_state(state):
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def managed_model_line(profile, model):
    return 'model = "%s"  %s' % (model, MARKER)


def switch(profile):
    resolve_providers()
    if profile not in PROFILE_PROVIDER:
        die("unknown profile %r (use mixed|glm|deepseek|official)" % profile)
    provider = PROFILE_PROVIDER[profile]
    if profile in ("glm", "deepseek") and not provider:
        die("no vendor block with a glm/deepseek prefix found in router_config.json")

    if not os.path.exists(CONFIG_PATH):
        die("config.toml not found at %s" % CONFIG_PATH)

    text = open(CONFIG_PATH, encoding="utf-8").read()
    import re
    if profile == "official":
        vendors, cfg = load_vendors()
        known = {"model_providers.ROUTER"} | {"model_providers.%s" % v["key_config_section"] for v in cfg["vendors"]}
        clean = drop_top_level_keys(strip_managed_sections(text, known), ("model_provider", "model_catalog_json"))
        clean = re.sub(r"^\s*model\s*=.*%s.*\n" % re.escape(MARKER), "", clean, flags=re.M)
        open(CONFIG_PATH, "w", encoding="utf-8", newline="\n").write(clean)
        validate()
        print("profile: official (anycodex keys removed from config.toml)")
        return

    # remember the user's original default model the first time we touch it
    state = read_state()
    if "original_model" not in state:
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
        if m:
            state["original_model"] = m.group(1)
            write_state(state)

    lines = text.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))
    head = "".join(lines[:first_table])
    rest = "".join(lines[first_table:])

    # drop any previous anycodex-managed model line and provider/model keys in the head
    head = re.sub(r"^\s*model\s*=.*%s.*\n" % re.escape(MARKER), "", head, flags=re.M)
    head = re.sub(r"^\s*(model_provider|model)\s*=.*\n", "", head, flags=re.M)
    head = head.rstrip("\n")
    model = state.get("original_model") if profile == "mixed" else DEFAULT_MODEL[profile]
    if not model:
        model = DEFAULT_MODEL[profile]
    new_head = 'model_provider = "%s"  %s\n%s\n' % (provider, MARKER, managed_model_line(profile, model))
    if head:
        new_head = head + "\n\n" + new_head
    open(CONFIG_PATH, "w", encoding="utf-8", newline="\n").write(new_head + rest)
    validate()
    label = "mixed (router: official + third-party)" if profile == "mixed" else "%s (direct third-party)" % provider
    print("profile: %s  ->  provider=%s, default model=%s" % (profile, provider, DEFAULT_MODEL[profile]))
    if state.get("original_model"):
        print("(your original default model %r is remembered in %s)" % (state["original_model"], os.path.basename(STATE_PATH)))


def validate():
    import tomllib
    cfg = tomllib.load(open(CONFIG_PATH, "rb"))
    assert cfg.get("model_provider"), "model_provider missing after rewrite"


def die(msg):
    print("ERROR:", msg)
    sys.exit(1)


def backup():
    if os.path.exists(CONFIG_PATH):
        bak = "%s.bak-anycodex-profile-%s" % (CONFIG_PATH, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(CONFIG_PATH, bak)
        print("backed up ->", os.path.basename(bak))


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    profile = sys.argv[1].strip().lower()
    backup()
    switch(profile)
    print("\nDone. FULLY QUIT and restart the ChatGPT desktop app to apply.")


if __name__ == "__main__":
    main()
