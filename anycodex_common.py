"""Shared helpers for AnyCodex setup/uninstall."""
import os

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".codex")

MARKER = "# managed by anycodex"


def strip_managed_sections(text, section_names=()):
    """Remove managed [section] blocks from TOML text.

    A block starts at a [header] line and runs until the next [header] or EOF.
    A block is dropped entirely (header + body) when either
      - its body contains the anycodex marker comment, or
      - its header is listed in section_names (exact match), which also cleans
        blocks written by earlier/external setups that carry no marker.
    Repeated installs therefore never leave duplicate declarations behind.
    Returns the cleaned text.
    """
    wanted = {"[%s]" % n for n in section_names}
    lines = text.splitlines(keepends=True)
    out = []
    block = []
    drop_block = False

    def flush():
        nonlocal block, drop_block
        if block and not drop_block:
            out.extend(block)
        block = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            flush()
            drop_block = stripped in wanted
            block = [line]
        else:
            if not block:  # top-level keys before any section
                out.append(line)
                continue
            block.append(line)
            if MARKER in line:
                drop_block = True
    flush()
    return "".join(out)


def drop_top_level_keys(text, keys):
    """Remove top-level key assignments (before the first [section] line)."""
    lines = text.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))
    keep = []
    for i, l in enumerate(lines):
        if i < first_table:
            s = l.strip()
            if any(s.startswith(k + " ") or s.startswith(k + "=") for k in keys):
                continue
        keep.append(l)
    return "".join(keep)
