"""AnyCodex local router.

Listens on 127.0.0.1:<port> and routes Codex Responses-API requests by the
`model` field in each request body:

  - models matching a vendor prefix (see router_config.json) are forwarded to
    that vendor's endpoint with the vendor API key;
  - everything else is forwarded to the ChatGPT backend with the user's own
    ChatGPT credentials passed through untouched (the engine sends them because
    the provider is declared with requires_openai_auth = true).

It also keeps the desktop app's model picker populated: the picker's base list
comes from a server-provided cache file (models_cache.json) that the app
refreshes on startup. A background thread re-injects the configured custom
models into that cache every time the app refreshes it.

Configuration is read from router_config.json located next to this file.
Keys are read from ~/.codex/config.toml ([model_providers.<section>]
experimental_bearer_token) with fallback to the environment variable.

Run with pythonw (no console) on Windows. Single instance is guarded by the
port bind. Log: <CODEX_HOME>/router.log
"""
import http.client
import http.server
import json
import os
import socket
import ssl
import sys
import threading
import time

try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(SCRIPT_DIR, "router_config.json"), encoding="utf-8") as f:
        CONFIG = json.load(f)
except (OSError, ValueError) as e:
    print("failed to load router_config.json: %r" % e)
    sys.exit(1)

PORT = CONFIG.get("port", 8231)
OFFICIAL = CONFIG["official"]
VENDORS = CONFIG["vendors"]

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".codex")
LOG_PATH = os.path.join(CODEX_HOME, "router.log")
CACHE_PATH = os.path.join(CODEX_HOME, "models_cache.json")

_log_lock = threading.Lock()


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with _log_lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def load_key(section, env_name):
    """Vendor key: ~/.codex/config.toml [model_providers.<section>] first, env fallback."""
    path = os.path.join(CODEX_HOME, "config.toml")
    try:
        import tomllib
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
        token = cfg["model_providers"][section]["experimental_bearer_token"]
        if token:
            return token
    except Exception:
        pass
    val = os.environ.get(env_name)
    if val:
        return val
    raise RuntimeError("no API key found for vendor section %r (env %r)" % (section, env_name))


KEYS = {}


def system_http_proxy():
    """(host, port) of the Windows system HTTP proxy, or None."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        if "=" in server:  # per-protocol form: http=h:p;https=h:p
            for part in server.split(";"):
                if part.startswith("https=") or part.startswith("http="):
                    server = part.split("=", 1)[1]
                    break
        host, _, port = server.partition(":")
        return (host, int(port or 8080))
    except Exception:
        return None


def tls_connection(host, use_system_proxy):
    """HTTPS connection to host, optionally tunneled through the system proxy."""
    proxy = system_http_proxy() if use_system_proxy else None
    if proxy:
        raw = socket.create_connection(proxy, timeout=30)
        raw.sendall(("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n" % (host, host)).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = raw.recv(4096)
            if not chunk:
                raise IOError("proxy closed during CONNECT")
            resp += chunk
        if b" 200 " not in resp.split(b"\r\n", 1)[0]:
            raise IOError("proxy CONNECT failed: %s" % resp.split(b"\r\n", 1)[0])
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPSConnection(host, timeout=600)
        conn.sock = sock
        return conn
    return http.client.HTTPSConnection(host, timeout=600)


def vendor_for(model):
    for v in VENDORS:
        for prefix in v["match_prefixes"]:
            if model.startswith(prefix):
                return v
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            body = b""
            while True:
                size_line = self.rfile.readline(64).strip()
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline(64)
                    return body
                body += self.rfile.read(size)
                self.rfile.readline(64)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _proxy(self):
        body = self._read_body()
        model = ""
        try:
            model = str(json.loads(body.decode("utf-8")).get("model", ""))
        except Exception:
            pass
        vendor = vendor_for(model)
        try:
            if vendor is not None:
                upstream_path = vendor["path_prefix"] + self.path
                host = vendor["host"]
                conn = tls_connection(host, vendor.get("use_system_proxy", False))
                headers = {
                    "Host": host,
                    "Authorization": "Bearer %s" % KEYS[vendor["name"]],
                    "Content-Type": self.headers.get("Content-Type", "application/json"),
                    "Accept": self.headers.get("Accept", "text/event-stream"),
                    "User-Agent": self.headers.get("User-Agent", "anycodex-router"),
                }
            else:
                upstream_path = OFFICIAL["prefix"] + self.path
                host = OFFICIAL["host"]
                conn = tls_connection(host, OFFICIAL.get("use_system_proxy", False))
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("host", "content-length", "connection",
                                                "transfer-encoding", "accept-encoding")}
                headers["Host"] = host
                headers["Accept-Encoding"] = "identity"
            conn.request(self.command, upstream_path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            route = vendor["name"] if vendor else "official"
            log("%s %s model=%s route=%s UPSTREAM_ERROR %r" % (self.command, self.path, model, route, e))
            payload = json.dumps({"error": {"message": "anycodex-router upstream error: %r" % e}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return

        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "content-length", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        total = 0
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:
            route = vendor["name"] if vendor else "official"
            log("%s %s model=%s route=%s status=%s STREAM_ABORTED after %dB %r"
                % (self.command, self.path, model, route, resp.status, total, e))
        finally:
            conn.close()
        route = vendor["name"] if vendor else "official"
        log("%s %s model=%s route=%s status=%s bytes=%d"
            % (self.command, self.path, model, route, resp.status, total))
        self.close_connection = True

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _proxy


# ---------- models_cache injection (keeps custom models in the desktop picker) ----------

def inject_models_cache():
    """Append configured custom models to the server-provided picker cache."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        models = data.get("models") or []
        have = {m.get("slug") for m in models}
        wanted = [m for v in VENDORS for m in v["models"]]
        missing = [m["slug"] for m in wanted if m["slug"] not in have]
        if not missing:
            return False
        tpl = next((m for m in models if m.get("slug") == "gpt-5.6-terra"), None)
        if tpl is None:
            log("models_cache: no template entry found, skip inject")
            return False
        base_prio = max((m.get("priority") or 0) for m in models) + 1
        for i, spec in enumerate(wanted):
            if spec["slug"] in have:
                continue
            entry = json.loads(json.dumps(tpl))  # clone official entry shape
            entry.update({
                "slug": spec["slug"],
                "display_name": spec["display_name"],
                "description": spec["description"],
                "priority": base_prio + i,
                "default_reasoning_level": spec["default_reasoning_level"],
                "supported_reasoning_levels": spec["supported_reasoning_levels"],
                "context_window": spec["context_window"],
                "max_context_window": spec["context_window"],
                "input_modalities": spec["input_modalities"],
                "visibility": "list",
                "is_default": False,
                "upgrade": None,
                "availability_nux": None,
                "additional_speed_tiers": [],
                "service_tiers": [],
                "default_service_tier": None,
                "available_access_programs": None,
                "multi_agent_version": None,
                "model_messages": None,
                "use_responses_lite": False,
                "supports_search_tool": False,
            })
            models.append(entry)
        data["models"] = models
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
        log("models_cache injected: %s" % ", ".join(missing))
        return True
    except Exception as e:
        log("models_cache inject error: %r" % e)
        return False


def cache_watcher():
    last = None
    while True:
        try:
            mt = os.stat(CACHE_PATH).st_mtime if os.path.exists(CACHE_PATH) else None
            if mt != last:
                if last is not None:
                    time.sleep(0.5)  # let the writer finish
                inject_models_cache()
                last = os.stat(CACHE_PATH).st_mtime if os.path.exists(CACHE_PATH) else None
        except Exception:
            pass
        time.sleep(2)


def main():
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 5 * 1024 * 1024:
        os.replace(LOG_PATH, LOG_PATH + ".old")
    for v in VENDORS:
        KEYS[v["name"]] = load_key(v["key_config_section"], v["key_env"])
    threading.Thread(target=cache_watcher, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    log("anycodex router started on 127.0.0.1:%d (vendors=%s, system proxy=%s)"
        % (PORT, ",".join(v["name"] for v in VENDORS), system_http_proxy()))
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except OSError as e:
        if "10048" in str(e) or "Only one usage" in str(e) or "98" in str(e):
            print("anycodex router already running, exiting.")
            sys.exit(0)
        raise
