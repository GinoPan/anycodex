"""AnyCodex local router.

Listens on 127.0.0.1:<port> and routes Codex Responses-API requests by the
`model` field in each request body:

  - models matching a vendor prefix (see router_config.json) are forwarded to
    that vendor's endpoint with the vendor API key;
  - everything else is forwarded to the ChatGPT backend with the user's own
    ChatGPT credentials passed through untouched (the engine sends them because
    the provider is declared with requires_openai_auth = true).

It merges custom model metadata into official GET /models responses, so the
picker always receives the latest official list plus the configured vendors.
A background thread also upserts those models into models_cache.json for
offline/cache-only reads. No startup-only model_catalog_json override is used.

Configuration is read from router_config.json located next to this file.
Keys are read from Windows Credential Manager with environment-variable and
legacy config.toml fallbacks.

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

from anycodex_credentials import read_credential
from anycodex_models import merge_model_catalog

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
DIAGNOSTICS = CONFIG.get("diagnostics") or {}
QUOTA_DIAGNOSTICS = DIAGNOSTICS.get("quota", True)

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".codex")
LOG_PATH = os.path.join(CODEX_HOME, "router.log")
CACHE_PATH = os.path.join(CODEX_HOME, "models_cache.json")
AUTH_PATH = os.path.join(CODEX_HOME, "auth.json")

_log_lock = threading.Lock()


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with _log_lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


_QUOTA_TERMS = ("rate", "limit", "quota", "usage", "credit", "remaining", "reset", "used")
_SENSITIVE_TERMS = ("token", "authorization", "cookie", "accountid", "email", "userid", "loginid")


def quota_response_headers(headers):
    """Return rate/usage response headers only; never include auth or cookies."""
    found = []
    for name, value in headers:
        lower = name.lower().replace("-", "")
        if any(term in lower for term in _SENSITIVE_TERMS):
            continue
        if any(term in lower for term in _QUOTA_TERMS):
            found.append("%s=%s" % (name, str(value)[:160]))
    return found


def quota_payload_summary(payload):
    """Extract quota-shaped scalar fields from JSON without logging user content."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        return []
    found = []

    def walk(value, path=""):
        if len(found) >= 40:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                next_path = "%s.%s" % (path, key) if path else str(key)
                walk(child, next_path)
        elif isinstance(value, list):
            for i, child in enumerate(value[:8]):
                walk(child, "%s[%d]" % (path, i))
        else:
            normalized = path.lower().replace("_", "").replace("-", "")
            if not any(term in normalized for term in _QUOTA_TERMS):
                return
            leaf = normalized.rsplit(".", 1)[-1]
            rendered = "<redacted>" if any(term in leaf for term in _SENSITIVE_TERMS) else repr(value)[:160]
            found.append("%s=%s" % (path, rendered))

    walk(data)
    return found


def quota_probe_path(path):
    lower = path.lower()
    return any(term in lower for term in ("usage", "limit", "quota", "credit", "account"))


def load_key(section, env_name):
    """Vendor key: Credential Manager, environment, then legacy config fallback."""
    val = read_credential(section)
    if val:
        return val
    val = os.environ.get(env_name)
    if val:
        return val
    # Backward compatibility only. setup.py migrates and removes these values.
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
    raise RuntimeError("no API key found for credential %r (env %r)" % (section, env_name))


KEYS = {}
ACTIVE_VENDORS = []


def vendor_for(model):
    for v in ACTIVE_VENDORS:
        for prefix in v["match_prefixes"]:
            if model.startswith(prefix):
                return v
    return None


def sanitize_official_input(body):
    """Normalize input items so the official backend accepts vendor-mixed threads.

    Probed 2026-09-27 against chatgpt.com/backend-api/codex/responses (gpt-5.6-sol,
    store=false). The backend validates every input item against codex schemas:

      - `reasoning` items must not carry raw thinking in `content` (400 "array
        too long ... maximum length 0"); ids must begin with 'rs'; an id the
        backend did not issue is a 404 item-not-found unless accompanied by its
        own Fernet `encrypted_content` (which always starts with "gAAAA").
      - `message` ids must begin with 'msg' (GLM/DeepSeek issue UUIDs or their
        own msg_resp_* ids).
      - vendor ids/passthrough fields appear on every item type.

    GLM/DeepSeek turns produce exactly the rejected shapes. So, for the
    official route only, rebuild each known item type from its semantic fields
    (dropping ids, vendor encrypted_content and passthrough metadata; call_id
    is kept everywhere - the client mints it, so the backend accepts any
    value). Vendor reasoning is converted to plain summary_text entries, which
    the backend accepts and the model reads. Official-issued reasoning items
    (Fernet encrypted_content + rs id) pass through untouched. Unknown item
    types also pass through untouched.

    Returns (new_body_or_None, items_rewritten).
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None, 0
    items = data.get("input")
    if not isinstance(items, list):
        return None, 0

    def reasoning_texts(item):
        texts = [t.get("text") for t in (item.get("summary") or [])
                 if isinstance(t, dict) and t.get("type") == "summary_text" and t.get("text")]
        if not texts:
            for t in item.get("content") or []:
                if isinstance(t, str) and t:
                    texts.append(t)
                elif isinstance(t, dict) and t.get("text"):
                    texts.append(t["text"])
        return [t for t in texts if t]

    def content_blocks(item):
        blocks = []
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") and c.get("text") is not None:
                blocks.append({"type": c["type"], "text": c["text"]})
            else:
                blocks.append(c)
        return blocks

    rewritten = 0
    kept = []
    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        t = item.get("type")
        if t == "reasoning":
            enc = item.get("encrypted_content")
            if (isinstance(enc, str) and enc.startswith("gAAAA")
                    and str(item.get("id") or "").startswith("rs")):
                kept.append(item)  # official-issued; backend resolves it
                continue
            texts = reasoning_texts(item)
            if texts:
                kept.append({"type": "reasoning",
                             "summary": [{"type": "summary_text", "text": x} for x in texts]})
            rewritten += 1
        elif t == "message":
            kept.append({"type": "message", "role": item.get("role"),
                         "content": content_blocks(item)})
            rewritten += 1
        elif t == "function_call":
            kept.append({"type": "function_call", "name": item.get("name"),
                         "call_id": item.get("call_id"), "arguments": item.get("arguments")})
            rewritten += 1
        elif t in ("function_call_output", "custom_tool_call_output"):
            kept.append({"type": t, "call_id": item.get("call_id"),
                         "output": item.get("output")})
            rewritten += 1
        elif t == "custom_tool_call":
            kept.append({"type": "custom_tool_call", "name": item.get("name"),
                         "call_id": item.get("call_id"), "input": item.get("input"),
                         "status": item.get("status")})
            rewritten += 1
        else:
            kept.append(item)
    if not rewritten:
        return None, 0
    data["input"] = kept
    return json.dumps(data, ensure_ascii=False).encode("utf-8"), rewritten


def chatgpt_auth():
    """(access_token, account_id) from auth.json, re-read per call.

    Used only as a fallback when the engine did not send ChatGPT credentials.
    With requires_openai_auth the engine's headers are preferred, including
    when credentials live in the OS keychain instead of auth.json.
    """
    try:
        with open(AUTH_PATH, encoding="utf-8") as f:
            d = json.load(f)
        tok = d["tokens"]["access_token"]
        return (tok, d["tokens"].get("account_id")) if tok else (None, None)
    except Exception as e:
        log("auth.json read failed: %r" % e)
        return None, None


def official_request_headers(request_headers, host):
    """Build official upstream headers while preserving engine credentials."""
    headers = {k: v for k, v in request_headers
               if k.lower() not in ("host", "content-length", "connection",
                                    "transfer-encoding", "accept-encoding")}
    headers["Host"] = host
    headers["Accept-Encoding"] = "identity"
    if not any(k.lower() == "authorization" for k in headers):
        tok, acc = chatgpt_auth()
        if tok:
            headers["Authorization"] = "Bearer %s" % tok
            if acc and not any(k.lower() == "chatgpt-account-id" for k in headers):
                headers["chatgpt-account-id"] = acc
    return headers


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

    def _health(self):
        payload = json.dumps({
            "ok": True,
            "service": "anycodex-router",
            "vendors": [v["name"] for v in ACTIVE_VENDORS],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _proxy(self):
        body = self._read_body()
        models_request = (self.command == "GET"
                          and self.path.split("?", 1)[0] == "/models")
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
                headers = official_request_headers(self.headers.items(), host)
                if models_request:
                    # The official ETag describes the unmodified catalog. Ask
                    # for the body so vendor edits also take effect on refresh.
                    headers = {k: v for k, v in headers.items()
                               if k.lower() not in ("if-none-match", "if-modified-since")}
                if body:
                    new_body, rewritten = sanitize_official_input(body)
                    if new_body is not None:
                        log("official route: rewrote %d input item(s) "
                            "(vendor CoT/ids normalized)" % rewritten)
                        body = new_body
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

        if models_request and resp.status == 200:
            self._models_response(resp, conn)
            return

        response_headers = resp.getheaders()
        if QUOTA_DIAGNOSTICS:
            quota_headers = quota_response_headers(response_headers)
            if quota_headers:
                log("quota headers path=%s status=%s %s"
                    % (self.path, resp.status, " ".join(quota_headers)))

        self.send_response(resp.status, resp.reason)
        for k, v in response_headers:
            if k.lower() in ("transfer-encoding", "content-length", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        total = 0
        err_snippet = ""
        quota_probe = bytearray()
        inspect_quota = (QUOTA_DIAGNOSTICS and
                         (resp.status >= 400 or quota_probe_path(self.path)))
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if resp.status >= 400 and not err_snippet:
                    err_snippet = chunk[:400].decode("utf-8", "replace").replace("\n", " ")
                if inspect_quota and len(quota_probe) < 512 * 1024:
                    quota_probe.extend(chunk[:512 * 1024 - len(quota_probe)])
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:
            route = vendor["name"] if vendor else "official"
            log("%s %s model=%s route=%s status=%s STREAM_ABORTED after %dB %r"
                % (self.command, self.path, model, route, resp.status, total, e))
        finally:
            conn.close()
        if quota_probe:
            quota_fields = quota_payload_summary(bytes(quota_probe))
            if quota_fields:
                log("quota payload path=%s status=%s %s"
                    % (self.path, resp.status, " ".join(quota_fields)))
        route = vendor["name"] if vendor else "official"
        log("%s %s model=%s route=%s status=%s bytes=%d%s"
            % (self.command, self.path, model, route, resp.status, total,
               (" err=%s" % err_snippet) if err_snippet else ""))
        self.close_connection = True

    def _models_response(self, resp, conn):
        """Merge before the client receives /models; never buffer Responses SSE."""
        try:
            payload = resp.read()
            try:
                data = json.loads(payload.decode("utf-8"))
                merged = merge_model_catalog(data, ACTIVE_VENDORS)
                payload = json.dumps(merged, ensure_ascii=False).encode("utf-8")
                log("models response merged: official=%d total=%d"
                    % (len(data["models"]), len(merged["models"])))
            except (ValueError, KeyError, TypeError) as e:
                # Unknown server schemas still pass through intact, rather
                # than breaking official model discovery.
                log("models response merge skipped: %s" % e)
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in ("content-length", "transfer-encoding", "connection",
                                       "etag", "content-encoding", "content-type"):
                    self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        finally:
            conn.close()
            self.close_connection = True

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/healthz":
            self._health()
        else:
            self._proxy()

    do_POST = do_DELETE = do_PUT = do_PATCH = _proxy


# ---------- models_cache injection (keeps custom models in the desktop picker) ----------

def inject_models_cache():
    """Upsert vendor metadata into the cache, preserving official freshness."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            snapshot = f.read()
        data = json.loads(snapshot)
        merged = merge_model_catalog(data, ACTIVE_VENDORS)
        if merged == data:
            return False
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False)
        # Do not replace a newer official refresh that landed while we were
        # constructing the merged cache. The watcher retries on the next poll.
        with open(CACHE_PATH, encoding="utf-8") as f:
            if f.read() != snapshot:
                os.remove(tmp)
                return False
        os.replace(tmp, CACHE_PATH)
        log("models_cache injected/updated: %s"
            % ", ".join(m["slug"] for v in ACTIVE_VENDORS for m in v["models"]))
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
                # Remember the pre-injection version so a concurrent official
                # refresh isn't swallowed by reading its mtime after merging.
                last = mt
        except Exception:
            pass
        time.sleep(2)


def main():
    global ACTIVE_VENDORS
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 5 * 1024 * 1024:
        os.replace(LOG_PATH, LOG_PATH + ".old")
    for v in VENDORS:
        try:
            KEYS[v["name"]] = load_key(v["key_config_section"], v["key_env"])
            ACTIVE_VENDORS.append(v)
        except RuntimeError as e:
            log("vendor %s disabled (no API key): %s" % (v["name"], e))
    if not ACTIVE_VENDORS:
        print("no vendor has an API key - nothing to route, exiting.")
        sys.exit(1)
    threading.Thread(target=cache_watcher, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    log("anycodex router started on 127.0.0.1:%d (vendors=%s, system proxy=%s)"
        % (PORT, ",".join(v["name"] for v in ACTIVE_VENDORS), system_http_proxy()))
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except OSError as e:
        if "10048" in str(e) or "Only one usage" in str(e) or "98" in str(e):
            print("anycodex router already running, exiting.")
            sys.exit(0)
        raise
