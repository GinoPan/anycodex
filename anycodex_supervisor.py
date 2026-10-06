"""Keep the AnyCodex router alive for the current Windows login session."""
import json
import os
import socket
import subprocess
import sys
import time


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(SCRIPT_DIR, "router_config.json"), encoding="utf-8") as f:
    CONFIG = json.load(f)

PORT = int(CONFIG.get("port", 8231))
SUPERVISOR_PORT = int(CONFIG.get("supervisor_port", PORT + 1))
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".codex")
LOG_PATH = os.path.join(CODEX_HOME, "router-supervisor.log")
ROUTER_PATH = os.path.join(SCRIPT_DIR, "codex_router.py")


def log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def port_open(port):
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def main():
    # Holding this listener is a dependency-free single-instance lock and gives
    # uninstall.py a precise process to stop before it stops the router.
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", SUPERVISOR_PORT))
        guard.listen(1)
    except OSError:
        return

    log("supervisor started (router_port=%d guard_port=%d)" % (PORT, SUPERVISOR_PORT))
    while True:
        if port_open(PORT):
            time.sleep(2)
            continue
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            child = subprocess.Popen(
                [sys.executable, ROUTER_PATH],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=flags,
            )
            log("spawned router pid=%d" % child.pid)
            code = child.wait()
            log("router exited code=%s; restarting" % code)
        except Exception as e:
            log("router spawn failed: %r" % e)
        time.sleep(2)


if __name__ == "__main__":
    main()
