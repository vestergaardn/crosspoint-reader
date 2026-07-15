#!/usr/bin/env python3
"""
CrossPoint Reader Sync — push EPUBs / PDFs to the reader over WiFi.

Two ways to send, both funnel into the same uploader:
  1. Watched folder  — drop a file into ~/Drop-to-Reader and it uploads itself.
  2. Local website   — open http://localhost:8765 and drag files onto the page.

The reader hosts an HTTP upload endpoint (see src/network/CrossPointWebServer.cpp:146):
    POST http://<host>/upload?path=/<dest>      (multipart file body)
'path' is a QUERY parameter and defaults to the SD root '/'.

The browser only ever talks to this local program (localhost), which forwards to
the reader server-side — so there is no CORS problem and nothing to configure.

Requirements: Python 3 (ships with macOS). No pip packages.

Usage:
    python3 reader_sync.py
    python3 reader_sync.py --host 192.168.1.42 --dest /Books --watch ~/Books-to-send

On the reader: open the wireless-transfer / web-server screen so the server is
running, then drop files here. Reach it at http://crosspoint.local by default.
"""

import argparse
import html
import json
import mimetypes
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- Defaults ---------------------------------------------------------------

DEFAULT_HOST = "crosspoint.local"       # reader mDNS name (works in AP + STA mode)
DEFAULT_DEST = "/"                       # SD-card destination folder
DEFAULT_UI_PORT = 8765                   # local website port
DEFAULT_WATCH = os.path.expanduser("~/Drop-to-Reader")
ALLOWED_EXT = {".epub", ".pdf"}
UPLOAD_TIMEOUT = 600                     # seconds; e-ink SD writes over WiFi are slow
POLL_SECONDS = 3                         # folder scan interval
STABLE_POLLS = 2                         # size must hold steady this many scans first
RETRY_SECONDS = 15                       # re-attempt a pending file no more often than this

# ---- Shared config + event feed ---------------------------------------------


class State:
    """Runtime config the web UI can tweak, plus a small event log both the
    folder watcher and the browser uploads write to so the UI shows everything."""

    def __init__(self, host, dest, watch):
        self.lock = threading.Lock()          # serialise: reader handles ONE upload at a time
        self.cfg_lock = threading.Lock()
        self.host = host
        self.dest = dest
        self.watch = watch
        self._events = []                     # bounded list of dicts
        self._next_id = 1

    def get_host(self):
        with self.cfg_lock:
            return self.host

    def get_dest(self):
        with self.cfg_lock:
            return self.dest

    def set_host(self, host):
        with self.cfg_lock:
            self.host = host.strip()

    def set_dest(self, dest):
        dest = dest.strip() or "/"
        if not dest.startswith("/"):
            dest = "/" + dest
        with self.cfg_lock:
            self.dest = dest

    def emit(self, name, status, message, source):
        with self.cfg_lock:
            ev = {
                "id": self._next_id,
                "ts": time.time(),
                "name": name,
                "status": status,          # queued | uploading | done | error
                "message": message,
                "source": source,          # web | folder
            }
            self._next_id += 1
            self._events.append(ev)
            if len(self._events) > 200:
                self._events = self._events[-200:]
            return ev["id"]

    def update(self, event_id, status, message):
        with self.cfg_lock:
            for ev in reversed(self._events):
                if ev["id"] == event_id:
                    ev["status"] = status
                    ev["message"] = message
                    break

    def events_since(self, since):
        with self.cfg_lock:
            return [e for e in self._events if e["id"] > since]


# ---- Reader communication ---------------------------------------------------


def build_multipart(field_name, filename, data):
    """Standard multipart/form-data body, exactly what a browser <form> sends."""
    boundary = "----CrossPointSync" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n" % (boundary, field_name, filename, ctype)
    ).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return boundary, head + data + tail


def reader_online(host, timeout=3):
    """Health check via the /api/status route (CrossPointWebServer.cpp:141)."""
    url = "http://%s/api/status" % host
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def upload_to_reader(state, filename, data):
    """Forward one file to the reader. Serialised: the ESP32 web server is
    single-threaded and processes one upload at a time."""
    host = state.get_host()
    dest = state.get_dest()
    boundary, body = build_multipart("file", filename, data)
    url = "http://%s/upload?path=%s" % (host, urllib.parse.quote(dest))
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("Content-Length", str(len(body)))
    req.add_header("Connection", "close")
    with state.lock:
        with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace").strip()
            return resp.status, text


def send_file(state, filename, data, source):
    """Emit lifecycle events around one upload attempt. Returns (ok, message)."""
    size_kb = len(data) / 1024.0
    eid = state.emit(filename, "uploading", "%.0f KB → %s" % (size_kb, state.get_host()), source)
    try:
        status, text = upload_to_reader(state, filename, data)
        if status == 200:
            state.update(eid, "done", text or "Uploaded")
            return True, text or "Uploaded"
        state.update(eid, "error", "HTTP %d: %s" % (status, text))
        return False, "HTTP %d: %s" % (status, text)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace").strip()
        except Exception:
            pass
        msg = "HTTP %d: %s" % (e.code, detail or e.reason)
        state.update(eid, "error", msg)
        return False, msg
    except Exception as e:  # URLError, timeout, DNS/mDNS failure, etc.
        msg = "%s (is the reader on the transfer screen?)" % e
        state.update(eid, "error", msg)
        return False, msg


# ---- Folder watcher ---------------------------------------------------------


def watch_folder(state):
    """Poll the drop folder; upload new stable files; move sent ones into Sent/."""
    watch = state.watch
    os.makedirs(watch, exist_ok=True)
    sent_dir = os.path.join(watch, "Sent")
    os.makedirs(sent_dir, exist_ok=True)

    pending = {}   # name -> {"sig": (size, mtime), "stable": int, "last_try": float}

    print("[folder] watching %s" % watch)
    while True:
        try:
            names = set()
            for name in os.listdir(watch):
                full = os.path.join(watch, name)
                if not os.path.isfile(full):
                    continue
                if os.path.splitext(name)[1].lower() not in ALLOWED_EXT:
                    continue
                names.add(name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size == 0:
                    continue
                sig = (st.st_size, int(st.st_mtime))
                rec = pending.get(name)
                if rec is None or rec["sig"] != sig:
                    pending[name] = {"sig": sig, "stable": 1, "last_try": 0.0}
                    continue
                rec["stable"] += 1
                # Wait until the size has held steady (finished copying), then
                # throttle retries so an offline reader isn't hammered.
                if rec["stable"] < STABLE_POLLS:
                    continue
                if time.time() - rec["last_try"] < RETRY_SECONDS:
                    continue
                rec["last_try"] = time.time()
                with open(full, "rb") as f:
                    data = f.read()
                ok, _msg = send_file(state, name, data, "folder")
                if ok:
                    dest = os.path.join(sent_dir, name)
                    if os.path.exists(dest):
                        base, ext = os.path.splitext(name)
                        dest = os.path.join(sent_dir, "%s-%d%s" % (base, int(time.time()), ext))
                    try:
                        shutil.move(full, dest)
                    except OSError:
                        pass
                    pending.pop(name, None)
            # forget files that disappeared
            for gone in [n for n in pending if n not in names]:
                pending.pop(gone, None)
        except Exception as e:
            print("[folder] error: %s" % e)
        time.sleep(POLL_SECONDS)


# ---- Local website ----------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Send to CrossPoint Reader</title>
<style>
  :root {
    --bg:#f4f5f7; --card:#fff; --fg:#1f2933; --muted:#7b8794;
    --accent:#6e9a82; --accent-d:#5a8c73; --border:#e4e7eb;
    --ok:#2f9e44; --err:#e03131; --busy:#1971c2;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1b1f24; --card:#242a31; --fg:#e8ebee; --muted:#9aa5b1;
            --border:#333b44; color-scheme: dark; }
  }
  * { box-sizing: border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         margin:0; background:var(--bg); color:var(--fg); }
  .wrap { max-width:720px; margin:0 auto; padding:28px 20px 60px; }
  h1 { font-size:1.4rem; margin:0 0 4px; }
  .status { display:flex; align-items:center; gap:8px; color:var(--muted);
            font-size:.9rem; margin-bottom:20px; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--muted); flex:none; }
  .dot.on { background:var(--ok); } .dot.off { background:var(--err); }
  .drop { background:var(--card); border:2px dashed var(--border); border-radius:14px;
          padding:46px 20px; text-align:center; cursor:pointer; transition:.15s;
          color:var(--muted); }
  .drop.hover { border-color:var(--accent); color:var(--accent);
                background:rgba(110,154,130,.08); }
  .drop strong { color:var(--fg); font-size:1.05rem; }
  .drop small { display:block; margin-top:6px; }
  .row { display:flex; gap:14px; margin:16px 0 4px; flex-wrap:wrap; align-items:center;
         font-size:.85rem; color:var(--muted); }
  .row label { display:flex; gap:6px; align-items:center; }
  .row input { background:var(--card); color:var(--fg); border:1px solid var(--border);
               border-radius:7px; padding:6px 9px; font-size:.85rem; }
  #host { width:180px; } #dest { width:120px; }
  h2 { font-size:.85rem; text-transform:uppercase; letter-spacing:.05em;
       color:var(--muted); margin:26px 0 10px; }
  ul { list-style:none; margin:0; padding:0; }
  li { background:var(--card); border:1px solid var(--border); border-radius:10px;
       padding:11px 14px; margin-bottom:8px; display:flex; align-items:center; gap:12px; }
  .name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .msg { font-size:.78rem; color:var(--muted); }
  .badge { font-size:.7rem; font-weight:600; padding:3px 9px; border-radius:20px; flex:none;
           color:#fff; text-transform:capitalize; }
  .badge.done { background:var(--ok); } .badge.error { background:var(--err); }
  .badge.uploading { background:var(--busy); } .badge.queued { background:var(--muted); }
  .src { font-size:.68rem; color:var(--muted); flex:none; }
  .empty { color:var(--muted); font-size:.9rem; }
  input[type=file] { display:none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Send to CrossPoint Reader</h1>
  <div class="status"><span id="dot" class="dot"></span><span id="statusText">Checking reader…</span></div>

  <label class="drop" id="drop" for="picker">
    <strong>Drop EPUB / PDF files here</strong>
    <small>or click to choose — files also sync from your Drop-to-Reader folder</small>
  </label>
  <input type="file" id="picker" accept=".epub,.pdf" multiple />

  <div class="row">
    <label>Reader <input id="host" /></label>
    <label>Folder on SD <input id="dest" /></label>
    <span id="watchHint"></span>
  </div>

  <h2>Activity</h2>
  <ul id="feed"><li class="empty">Nothing sent yet.</li></ul>
</div>

<script>
let since = 0;
const feed = document.getElementById('feed');
const items = new Map();       // id -> {name,status,message,source}
const drop = document.getElementById('drop');
const picker = document.getElementById('picker');

function render() {
  const list = [...items.values()].sort((a,b)=>b.id-a.id).slice(0,50);
  if (!list.length) { feed.innerHTML = '<li class="empty">Nothing sent yet.</li>'; return; }
  feed.innerHTML = list.map(e =>
    `<li><span class="name">${esc(e.name)}</span>`+
    `<span class="msg">${esc(e.message||'')}</span>`+
    `<span class="src">${e.source}</span>`+
    `<span class="badge ${e.status}">${e.status}</span></li>`).join('');
}
function esc(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

async function poll() {
  try {
    const r = await fetch('/events?since='+since);
    const j = await r.json();
    for (const e of j.events) { items.set(e.id, e); since = Math.max(since, e.id); }
    if (j.events.length) render();
    const dot = document.getElementById('dot');
    dot.className = 'dot ' + (j.reader_online ? 'on':'off');
    document.getElementById('statusText').textContent =
      (j.reader_online ? 'Reader online — ' : 'Reader not reachable — ') + j.host;
    if (document.activeElement !== document.getElementById('host'))
      document.getElementById('host').value = j.host;
    if (document.activeElement !== document.getElementById('dest'))
      document.getElementById('dest').value = j.dest;
    document.getElementById('watchHint').textContent = 'watching ' + j.watch;
  } catch (e) {}
}

async function sendFiles(files) {
  for (const f of files) {
    const ext = f.name.toLowerCase().slice(f.name.lastIndexOf('.'));
    if (ext !== '.epub' && ext !== '.pdf') continue;
    try {
      await fetch('/push', { method:'POST', body:f,
        headers:{ 'X-Filename': encodeURIComponent(f.name) } });
    } catch (e) {}
    await poll();
  }
}

['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add('hover'); }));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove('hover'); }));
drop.addEventListener('drop', e => sendFiles(e.dataTransfer.files));
picker.addEventListener('change', e => { sendFiles(picker.files); picker.value=''; });

function pushConfig(k,v){ fetch('/config',{method:'POST',
  headers:{'Content-Type':'application/json'}, body:JSON.stringify({[k]:v})}); }
document.getElementById('host').addEventListener('change', e=>pushConfig('host',e.target.value));
document.getElementById('dest').addEventListener('change', e=>pushConfig('dest',e.target.value));

poll(); setInterval(poll, 1000);
</script>
</body>
</html>
"""


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # keep the console quiet; we print our own lines

        def _send(self, code, ctype, body):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE)
            elif path == "/events":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                since = int((qs.get("since", ["0"])[0]) or 0)
                payload = {
                    "events": state.events_since(since),
                    "reader_online": reader_online(state.get_host(), timeout=2),
                    "host": state.get_host(),
                    "dest": state.get_dest(),
                    "watch": state.watch,
                }
                self._send(200, "application/json", json.dumps(payload))
            else:
                self._send(404, "text/plain", "not found")

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if path == "/push":
                raw = self.headers.get("X-Filename", "upload.bin")
                filename = urllib.parse.unquote(raw)
                ok, msg = send_file(state, filename, body, "web")
                self._send(200 if ok else 502, "application/json",
                           json.dumps({"ok": ok, "message": msg}))
            elif path == "/config":
                try:
                    cfg = json.loads(body.decode("utf-8"))
                except Exception:
                    cfg = {}
                if "host" in cfg:
                    state.set_host(cfg["host"])
                if "dest" in cfg:
                    state.set_dest(cfg["dest"])
                self._send(200, "application/json", json.dumps({"ok": True}))
            else:
                self._send(404, "text/plain", "not found")

    return Handler


# ---- Main -------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Sync EPUBs/PDFs to a CrossPoint Reader over WiFi.")
    ap.add_argument("--host", default=DEFAULT_HOST, help="reader hostname or IP (default: crosspoint.local)")
    ap.add_argument("--dest", default=DEFAULT_DEST, help="destination folder on the SD card (default: /)")
    ap.add_argument("--watch", default=DEFAULT_WATCH, help="folder to watch (default: ~/Drop-to-Reader)")
    ap.add_argument("--port", type=int, default=DEFAULT_UI_PORT, help="local website port (default: 8765)")
    ap.add_argument("--no-web", action="store_true", help="folder watcher only, no website")
    ap.add_argument("--no-watch", action="store_true", help="website only, no folder watcher")
    args = ap.parse_args()

    state = State(args.host, args.dest if args.dest.startswith("/") else "/" + args.dest,
                  os.path.expanduser(args.watch))

    print("CrossPoint Reader Sync")
    print("  Reader:       http://%s   (SD folder: %s)" % (state.host, state.dest))
    if not args.no_web:
        print("  Web UI:       http://localhost:%d" % args.port)
    if not args.no_watch:
        print("  Drop folder:  %s" % state.watch)
    print("  (open the wireless-transfer screen on the reader so its server is running)")
    print()

    online = reader_online(state.host, timeout=2)
    print("  Reader is %s at %s" % ("ONLINE" if online else "not reachable yet", state.host))
    print()

    if not args.no_watch:
        threading.Thread(target=watch_folder, args=(state,), daemon=True).start()

    if args.no_web:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nbye")
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        server.shutdown()


if __name__ == "__main__":
    main()
