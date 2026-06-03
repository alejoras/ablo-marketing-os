#!/usr/bin/env python3
"""
Ablo Studio Marketing OS — web server (for Railway, or any host).

Serves the static dashboard AND a password-gated /connections page where the
tokens for every connected source can be viewed (masked), tested live, and
edited. Stdlib only — no pip installs.

Secrets model:
  - Base tokens come from the process environment (Railway variables).
  - Edits are written to  <OS_DATA_DIR>/tokens.env  (a persistent volume),
    which overrides the base. Nothing secret is ever served unauthenticated
    or written into data.js / the repo.
  - build.py runs as a subprocess with {env, ...tokens.env} and writes
    data.js + history.jsonl into OS_DATA_DIR.

Env:
  PORT            (Railway sets this)            default 8080
  OS_DATA_DIR     writable dir for data + tokens default ./ (repo dir)
  ADMIN_USER      basic-auth user                default "admin"
  ADMIN_PASSWORD  basic-auth password — REQUIRED to enable /connections
"""
import base64
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("OS_DATA_DIR", HERE))
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKENS_FILE = DATA_DIR / "tokens.env"
STATE_FILE = DATA_DIR / "server-state.json"
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


# ---- token store (editable overrides on the volume) -------------------------
def read_tokens_file():
    out = {}
    if TOKENS_FILE.exists():
        for line in TOKENS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'").strip('"')
    return out


def write_tokens_file(updates):
    cur = read_tokens_file()
    cur.update({k: v for k, v in updates.items() if v})  # blank = keep existing
    TOKENS_FILE.write_text("".join(f"{k}={v}\n" for k, v in cur.items()))
    TOKENS_FILE.chmod(0o600)


def merged_env():
    return {**os.environ, **read_tokens_file()}


# ---- source registry + live tests -------------------------------------------
def _ping(url, headers=None, timeout=4):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (200 <= r.status < 300), str(r.status)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__


def t_posthog(env):
    key = env.get("POSTHOG_PERSONAL_API_KEY")
    if not key:
        return None
    pid = env.get("POSTHOG_PROJECT_ID", "419152")
    region = "eu" if "eu" in env.get("POSTHOG_HOST", "").lower() else "us"
    return _ping(f"https://{region}.posthog.com/api/projects/{pid}/experiments/?limit=1",
                 {"Authorization": f"Bearer {key}"})


def t_klaviyo(env):
    key = env.get("KLAVIYO_API_KEY_ABLO")
    if not key:
        return None
    return _ping("https://a.klaviyo.com/api/flows/?page%5Bsize%5D=1",
                 {"Authorization": f"Klaviyo-API-Key {key}", "revision": "2024-10-15", "accept": "application/json"})


def t_meta(env):
    key = env.get("META_ADS_TOKEN")
    if not key:
        return None
    return _ping(f"https://graph.facebook.com/v21.0/me?fields=id&access_token={key}")


def t_clickup(env):
    key = env.get("CLICKUP_TOKEN_ABLO")
    if not key:
        return None
    return _ping("https://api.clickup.com/api/v2/list/901415977874", {"Authorization": key})


def t_stripe(env):
    key = env.get("STRIPE_API_KEY")
    if not key:
        return None
    return _ping("https://api.stripe.com/v1/balance",
                 {"Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode()})


SOURCES = [
    {"id": "posthog", "label": "PostHog", "fields": ["POSTHOG_PERSONAL_API_KEY", "POSTHOG_PROJECT_ID", "POSTHOG_HOST"],
     "note": "Funnel, channel attribution, experiments, daily history.", "test": t_posthog},
    {"id": "klaviyo", "label": "Klaviyo", "fields": ["KLAVIYO_API_KEY_ABLO"],
     "note": "Lifecycle flows + prepared emails.", "test": t_klaviyo},
    {"id": "meta", "label": "Meta (Ads + Instagram)", "fields": ["META_ADS_TOKEN", "META_IG_TOKEN"],
     "note": "Campaign metrics + IG organic. META_IG_TOKEN (for agent posting) is separate and may be expired.", "test": t_meta},
    {"id": "clickup", "label": "ClickUp", "fields": ["CLICKUP_TOKEN_ABLO"],
     "note": "Live task feed — the action-item source of truth.", "test": t_clickup},
    {"id": "stripe", "label": "Stripe", "fields": ["STRIPE_API_KEY"],
     "note": "Revenue / paying customers / true CAC. Not connected yet — add the key to light it up.", "test": t_stripe},
]


def mask(v):
    if not v:
        return ""
    if len(v) <= 10:
        return "•" * len(v)
    return f"{v[:4]}…{v[-4:]}  ({len(v)} chars)"


# ---- build / refresh --------------------------------------------------------
_refreshing = threading.Lock()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except ValueError:
            pass
    return {}


def run_build():
    if not _refreshing.acquire(blocking=False):
        return False, "already refreshing"
    try:
        env = {**os.environ, **read_tokens_file()}
        env["OS_DATA_DIR"] = str(DATA_DIR)
        p = subprocess.run([sys.executable, str(HERE / "build.py")], env=env,
                           capture_output=True, text=True, timeout=300)
        ok = p.returncode == 0
        STATE_FILE.write_text(json.dumps({
            "last_refresh": datetime.now(timezone.utc).isoformat(),
            "ok": ok, "log": (p.stderr or p.stdout or "")[-1500:],
        }))
        return ok, p.stderr[-400:] if not ok else "ok"
    except Exception as e:
        return False, str(e)
    finally:
        _refreshing.release()


def scheduler():
    """Build on boot if needed, then refresh daily."""
    if not (DATA_DIR / "data.js").exists():
        run_build()
    while True:
        time.sleep(6 * 3600)
        st = load_state()
        last = st.get("last_refresh", "")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            age = 1e9
        if age > 20 * 3600:
            run_build()


# ---- HTTP handler -----------------------------------------------------------
STATIC = {"/": "index.html", "/index.html": "index.html", "/robots.txt": "robots.txt"}
CT = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".png": "image/png",
      ".css": "text/css", ".txt": "text/plain", ".ico": "image/x-icon", ".json": "application/json"}


def page(body, title="Connections"):
    return f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{title} · Ablo OS</title>
<style>
body{{font-family:Inter,system-ui,sans-serif;background:#0c0c0c;color:#fafafa;margin:0;padding:40px 20px;font-size:14px;line-height:1.6}}
.wrap{{max-width:720px;margin:0 auto}}
h1{{font-size:26px;font-weight:600;letter-spacing:-.02em;margin:0 0 6px}}
.sub{{color:#999;margin:0 0 26px}}
.src{{border:1px solid #2a2a2a;border-radius:14px;padding:18px 20px;margin-bottom:14px;background:rgba(255,255,255,.02)}}
.src h3{{margin:0 0 4px;font-size:15px;display:flex;align-items:center;gap:10px}}
.src .note{{color:#888;font-size:12.5px;margin:0 0 14px}}
.dot{{width:8px;height:8px;border-radius:50%;display:inline-block}}
.ok{{background:#4ade80}} .bad{{background:#f87171}} .na{{background:#555}}
.badge{{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:#999}}
label{{display:block;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:#c9a96e;margin:10px 0 4px}}
.cur{{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#777;margin-bottom:5px}}
input{{width:100%;box-sizing:border-box;background:#151515;border:1px solid #2a2a2a;border-radius:8px;color:#fafafa;padding:9px 12px;font-size:13px;font-family:ui-monospace,Menlo,monospace}}
input:focus{{outline:none;border-color:#c9a96e}}
.btn{{font-family:inherit;font-size:13px;font-weight:500;color:#e8d5a3;background:transparent;border:1px solid rgba(232,213,163,.4);padding:10px 18px;border-radius:999px;cursor:pointer}}
.btn:hover{{background:rgba(232,213,163,.1)}}
.btn.primary{{background:rgba(232,213,163,.14)}}
.row{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:8px}}
.flash{{border:1px solid rgba(74,222,128,.3);background:rgba(74,222,128,.08);color:#4ade80;border-radius:10px;padding:10px 14px;margin-bottom:18px;font-size:13px}}
a{{color:#c9a96e}}
</style></head><body><div class=wrap>{body}</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def _authed(self):
        if not ADMIN_PASSWORD:
            return None  # signal "not configured"
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, p = base64.b64decode(h[6:]).decode().split(":", 1)
                if hmac.compare_digest(u, ADMIN_USER) and hmac.compare_digest(p, ADMIN_PASSWORD):
                    return True
            except Exception:
                pass
        return False

    def _require_auth(self):
        a = self._authed()
        if a is None:
            self._send(503, page("<h1>Connections</h1><p class=sub>Set an <code>ADMIN_PASSWORD</code> "
                                  "environment variable to enable this page.</p>"))
            return False
        if not a:
            self._send(401, "Auth required", extra={"WWW-Authenticate": 'Basic realm="Ablo OS"'})
            return False
        return True

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/connections":
            return self._connections_page()
        # static
        if path in STATIC:
            return self._file(HERE / STATIC[path])
        if path == "/data.js":
            return self._file(DATA_DIR / "data.js")
        safe = path.lstrip("/").replace("..", "")
        for base in (HERE, DATA_DIR):
            f = base / safe
            if f.is_file():
                return self._file(f)
        return self._send(404, "not found", "text/plain")

    do_HEAD = do_GET

    def _file(self, f):
        if not f.is_file():
            return self._send(404, "not found", "text/plain")
        ctype = CT.get(f.suffix, "application/octet-stream")
        self._send(200, f.read_bytes(), ctype)

    def _connections_page(self, flash=""):
        if not self._require_auth():
            return
        env = merged_env()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(lambda s: s["test"](env), SOURCES))
        st = load_state()
        cards = []
        for s, res in zip(SOURCES, results):
            if res is None:
                dot, badge = "na", "not set"
            elif res[0]:
                dot, badge = "ok", "valid"
            else:
                dot, badge = "bad", "invalid · " + res[1]
            fields = "".join(
                f'<label>{f}</label><div class=cur>{mask(env.get(f, "")) or "— not set —"}</div>'
                f'<input name="{f}" type="password" autocomplete="off" placeholder="leave blank to keep current">'
                for f in s["fields"])
            cards.append(
                f'<div class=src><h3><span class="dot {dot}"></span>{s["label"]} '
                f'<span class=badge>{badge}</span></h3><p class=note>{s["note"]}</p>{fields}</div>')
        last = st.get("last_refresh", "never")[:19].replace("T", " ")
        body = (
            "<h1>Connections</h1>"
            "<p class=sub>View, test and update the API tokens for every connected source. "
            "Edits are saved to a private volume, never to the public site. "
            "Blank fields keep their current value.</p>"
            + (f"<div class=flash>{flash}</div>" if flash else "")
            + "<form method=post action=/connections>" + "".join(cards)
            + '<div class=row><button class="btn primary" type=submit>Save tokens</button>'
              '<button class=btn formaction=/refresh formmethod=post>Refresh data now</button>'
              f'<span class=badge>last refresh: {last} {"· ok" if st.get("ok") else ("· FAILED" if st else "")}</span></div>'
            "</form>"
            '<p style="margin-top:24px"><a href="/">← back to the dashboard</a></p>')
        self._send(200, page(body))

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._require_auth():
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else ""
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        if path == "/connections":
            allowed = {f for s in SOURCES for f in s["fields"]}
            write_tokens_file({k: v for k, v in form.items() if k in allowed and v.strip()})
            return self._connections_page(flash="Saved. Click ‘Refresh data now’ to rebuild with the new tokens.")
        if path == "/refresh":
            ok, msg = run_build()
            return self._connections_page(flash=("Refreshed ✓" if ok else "Refresh failed: " + msg))
        self._send(404, "not found", "text/plain")


def main():
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[server] Ablo OS on :{port} · data={DATA_DIR} · admin={'set' if ADMIN_PASSWORD else 'NOT SET'}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
