#!/usr/bin/env python3
"""
CrossPoint Reader Dashboard — a small control panel + staging server on your Mac.

You drop books and a home-screen image into cards on a local web page. This program
stages them in ~/CrossPointReader and publishes a manifest the reader pulls from over
WiFi (the firmware "sync on wake" feature), so the reader grabs new items by itself
when it wakes. A "Send now" button also pushes books immediately to a reader that's
awake on its transfer screen.

Staging layout (also usable straight from Finder):
    ~/CrossPointReader/books/     EPUBs to put on the reader
    ~/CrossPointReader/home/      one home-screen image (png/jpg/bmp)

Endpoints the reader calls (pull):
    GET /api/manifest             list of books + home image, with URLs + hashes
    GET /dl/books/<name>          download a staged book
    GET /dl/home/<name>           download the staged home image

Endpoints the web page uses (control):
    GET /  /api/state  POST /api/books  /api/home  /api/*/delete  /api/push  /api/config

Requirements: Python 3 (ships with macOS). No pip packages.
Runs on 0.0.0.0:8765 so the reader can reach it over WiFi — use on a trusted home network.
"""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- Paths / config ---------------------------------------------------------

BASE = os.path.expanduser("~/CrossPointReader")
BOOKS_DIR = os.path.join(BASE, "books")
HOME_DIR = os.path.join(BASE, "home")
CONFIG_PATH = os.path.join(BASE, "config.json")
WORK_DIR = os.path.join(BASE, ".work")

BOOK_EXT = {".epub"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp"}
# Formats Calibre's ebook-convert can turn into EPUB. Dropped files of these types are
# auto-converted (requires Calibre installed — https://calibre-ebook.com).
CONVERTIBLE_EXT = {".pdf", ".mobi", ".azw", ".azw3", ".fb2", ".cbz", ".cbr", ".docx", ".rtf", ".lit", ".pdb", ".htmlz"}
DEFAULT_PORT = 8765
DEFAULT_READER_HOST = "crosspoint.local"   # for the optional "Send now" push
PUSH_TIMEOUT = 600

_cfg_lock = threading.Lock()
_push_lock = threading.Lock()              # reader handles one upload at a time
_hash_cache = {}                           # abspath -> (mtime, size, sha1)


def ensure_dirs():
    os.makedirs(BOOKS_DIR, exist_ok=True)
    os.makedirs(HOME_DIR, exist_ok=True)


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"reader_host": DEFAULT_READER_HOST}


def save_config(cfg):
    with _cfg_lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)


def lan_ip():
    """Best-effort primary LAN IP, so we can tell the user what to type on the reader."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))          # no packets sent; just picks the route
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---- Staging helpers --------------------------------------------------------


def safe_name(name):
    """A plain basename, no path separators or traversal."""
    name = os.path.basename(name.replace("\\", "/"))
    if name in ("", ".", "..") or "/" in name:
        return None
    return name


def sha1_of(path):
    st = os.stat(path)
    key = (st.st_mtime, st.st_size)
    cached = _hash_cache.get(path)
    if cached and cached[:2] == key:
        return cached[2]
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _hash_cache[path] = (st.st_mtime, st.st_size, digest)
    return digest


def list_books():
    out = []
    for name in sorted(os.listdir(BOOKS_DIR)):
        full = os.path.join(BOOKS_DIR, name)
        if os.path.isfile(full) and os.path.splitext(name)[1].lower() in BOOK_EXT:
            out.append({"name": name, "size": os.path.getsize(full)})
    return out


def current_home():
    for name in sorted(os.listdir(HOME_DIR)):
        full = os.path.join(HOME_DIR, name)
        if os.path.isfile(full) and os.path.splitext(name)[1].lower() in IMAGE_EXT:
            return {"name": name, "size": os.path.getsize(full)}
    return None


def build_manifest(host):
    """Absolute URLs are built from the Host the reader connected to, so we never
    need to know our own address in advance."""
    books = []
    for b in list_books():
        full = os.path.join(BOOKS_DIR, b["name"])
        books.append({
            "name": b["name"],
            "url": "http://%s/dl/books/%s" % (host, urllib.parse.quote(b["name"])),
            "size": b["size"],
            "sha1": sha1_of(full),
        })
    home = None
    h = current_home()
    if h:
        full = os.path.join(HOME_DIR, h["name"])
        home = {
            "name": h["name"],
            "url": "http://%s/dl/home/%s" % (host, urllib.parse.quote(h["name"])),
            "sha1": sha1_of(full),
        }
    return {"version": 1, "books": books, "home": home}


# ---- Conversion (PDF / MOBI / … -> EPUB via Calibre) + background jobs ----------


def find_ebook_convert():
    """Locate Calibre's ebook-convert (PATH or the macOS app bundle). None if absent."""
    p = shutil.which("ebook-convert")
    if p:
        return p
    mac = "/Applications/calibre.app/Contents/MacOS/ebook-convert"
    return mac if os.path.isfile(mac) else None


_jobs = []
_jobs_lock = threading.Lock()


def add_job(name):
    with _jobs_lock:
        jid = uuid.uuid4().hex[:8]
        _jobs.append({"id": jid, "name": name, "status": "converting",
                      "message": "Converting to EPUB…", "ts": time.time()})
        if len(_jobs) > 50:
            del _jobs[:-50]
        return jid


def update_job(jid, status, message):
    with _jobs_lock:
        for j in _jobs:
            if j["id"] == jid:
                j["status"] = status
                j["message"] = message
                j["ts"] = time.time()
                break


def jobs_snapshot():
    with _jobs_lock:
        return list(_jobs)[-20:]


def convert_worker(src_path, base_name, jid):
    """Run ebook-convert to turn a staged temp file into books/<base>.epub."""
    conv = find_ebook_convert()
    out = os.path.join(BOOKS_DIR, base_name + ".epub")
    try:
        if not conv:
            raise RuntimeError("Calibre not found")
        subprocess.run([conv, src_path, out], check=True, capture_output=True, timeout=900)
        update_job(jid, "done", "Converted to EPUB")
    except subprocess.TimeoutExpired:
        update_job(jid, "error", "Conversion timed out")
    except subprocess.CalledProcessError as e:
        lines = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        update_job(jid, "error", "Conversion failed: " + (lines[-1] if lines else "see Calibre"))
    except Exception as e:
        update_job(jid, "error", "Conversion failed: %s" % e)
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass


# ---- Optional push (immediate, needs reader awake on transfer screen) --------


def push_book_to_reader(reader_host, filename, data):
    boundary = "----CrossPointSync" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n" % (boundary, filename, ctype)
    ).encode("utf-8")
    body = head + data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    url = "http://%s/upload?path=%s" % (reader_host, urllib.parse.quote("/"))
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("Connection", "close")
    with _push_lock:
        with urllib.request.urlopen(req, timeout=PUSH_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace").strip()


def reader_online(reader_host, timeout=2):
    try:
        with urllib.request.urlopen("http://%s/api/status" % reader_host, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ---- Web page ---------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>CrossPoint Reader</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --fg:#1f2933; --muted:#7b8794; --accent:#6e9a82;
          --accent-d:#5a8c73; --border:#e4e7eb; --ok:#2f9e44; --err:#e03131; --busy:#1971c2; }
  @media (prefers-color-scheme: dark) { :root { --bg:#1b1f24; --card:#242a31; --fg:#e8ebee;
          --muted:#9aa5b1; --border:#333b44; color-scheme: dark; } }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; margin:0;
         background:var(--bg); color:var(--fg); }
  .wrap { max-width:760px; margin:0 auto; padding:26px 20px 60px; }
  h1 { font-size:1.45rem; margin:0 0 2px; }
  .sub { color:var(--muted); font-size:.9rem; margin-bottom:22px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:14px;
          padding:20px; margin-bottom:18px; }
  .card h2 { font-size:1.05rem; margin:0 0 3px; }
  .card .hint { color:var(--muted); font-size:.85rem; margin:0 0 14px; }
  .drop { border:2px dashed var(--border); border-radius:11px; padding:26px 16px; text-align:center;
          color:var(--muted); cursor:pointer; transition:.15s; }
  .drop.hover { border-color:var(--accent); color:var(--accent); background:rgba(110,154,130,.08); }
  ul { list-style:none; margin:14px 0 0; padding:0; }
  li { display:flex; align-items:center; gap:12px; padding:10px 12px; border:1px solid var(--border);
       border-radius:9px; margin-bottom:8px; }
  .nm { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .sz { color:var(--muted); font-size:.8rem; flex:none; }
  .x { border:none; background:none; color:var(--muted); cursor:pointer; font-size:1.1rem; flex:none; }
  .x:hover { color:var(--err); }
  .empty { color:var(--muted); font-size:.88rem; }
  .msg { font-size:.78rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:230px; }
  .badge { font-size:.66rem; font-weight:600; padding:3px 9px; border-radius:20px; color:#fff; flex:none;
           text-transform:capitalize; }
  .badge.busy { background:var(--busy); } .badge.err { background:var(--err); }
  .thumb { display:flex; align-items:center; gap:14px; margin-top:14px; }
  .thumb img { max-height:120px; max-width:180px; border-radius:8px; border:1px solid var(--border); }
  .addr { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:16px 20px;
          margin-bottom:18px; font-size:.9rem; }
  .addr code { background:rgba(110,154,130,.13); color:var(--accent-d); padding:2px 7px; border-radius:5px;
               font-size:.95em; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; background:var(--muted);
         margin-right:6px; }
  .dot.on { background:var(--ok); } .dot.off { background:var(--err); }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:12px; }
  button.act { background:var(--accent); color:#fff; border:none; border-radius:8px; padding:9px 16px;
               font-size:.9rem; cursor:pointer; }
  button.act:hover { background:var(--accent-d); }
  button.act:disabled { background:var(--muted); cursor:default; }
  input.host { background:var(--bg); color:var(--fg); border:1px solid var(--border); border-radius:7px;
               padding:7px 9px; font-size:.85rem; width:190px; }
  .note { color:var(--muted); font-size:.8rem; margin-top:8px; }
  input[type=file] { display:none; }
</style></head>
<body><div class="wrap">
  <h1>CrossPoint Reader</h1>
  <div class="sub">Drop books and a home-screen image. Your reader picks them up over WiFi.</div>

  <div class="addr">
    <div><span id="rdot" class="dot"></span><span id="rtext">…</span></div>
    <div style="margin-top:8px">On the reader's <b>Sync</b> setting, enter this address:
      &nbsp;<code id="syncurl">…</code></div>
  </div>

  <div class="card">
    <h2>Books</h2>
    <p class="hint">EPUB — or PDF / MOBI / AZW3, auto-converted to EPUB. <span id="convStatus"></span></p>
    <label class="drop" id="dropBooks" for="pickBooks"><b>Drop books here</b><br><small>EPUB, PDF, MOBI, AZW3… — or click to choose</small></label>
    <input type="file" id="pickBooks" accept=".epub,.pdf,.mobi,.azw3,.azw,.fb2,.cbz,.cbr,.docx,.rtf" multiple />
    <ul id="booksList"></ul>
  </div>

  <div class="card">
    <h2>Home screen</h2>
    <p class="hint">One image (PNG/JPG/BMP) shown when the reader sleeps. Delivered on the next sync.</p>
    <label class="drop" id="dropHome" for="pickHome"><b>Drop an image here</b><br><small>or click to choose</small></label>
    <input type="file" id="pickHome" accept="image/*" />
    <div id="homeThumb"></div>
  </div>

  <div class="card">
    <h2>Send now <span style="font-weight:400;color:var(--muted);font-size:.85rem">(optional)</span></h2>
    <p class="hint">Push staged books immediately to a reader that's awake on its wireless-transfer screen.
       (The home image is delivered by sync-on-wake, which converts it on the device.)</p>
    <div class="row">
      <label>Reader <input class="host" id="readerHost" /></label>
      <button class="act" id="pushBtn">Send books now</button>
      <span id="pushMsg" class="note"></span>
    </div>
  </div>
</div>
<script>
const $ = s => document.querySelector(s);
function esc(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function fmt(n){ return n>=1048576 ? (n/1048576).toFixed(1)+' MB' : Math.max(1,Math.round(n/1024))+' KB'; }

async function refresh() {
  const j = await (await fetch('/api/state')).json();
  $('#syncurl').textContent = j.sync_url;
  const rdot=$('#rdot'), rtext=$('#rtext');
  rdot.className='dot '+(j.reader_online?'on':'off');
  rtext.textContent = j.reader_online ? ('Reader reachable now at '+j.reader_host)
                                      : ('Reader not reachable at '+j.reader_host+' (only needed for "Send now")');
  if (document.activeElement !== $('#readerHost')) $('#readerHost').value = j.reader_host;

  const activeJobs = (j.jobs||[]).filter(x => x.status==='converting' || x.status==='error');
  const jobsHtml = activeJobs.map(x =>
    `<li><span class="nm">${esc(x.name)}</span><span class="msg">${esc(x.message||'')}</span>`+
    `<span class="badge ${x.status==='error'?'err':'busy'}">${x.status==='error'?'error':'converting'}</span></li>`).join('');
  const booksHtml = j.books.map(b =>
    `<li><span class="nm">${esc(b.name)}</span><span class="sz">${fmt(b.size)}</span>`+
    `<button class="x" onclick="delBook('${encodeURIComponent(b.name)}')">✕</button></li>`).join('');
  $('#booksList').innerHTML = (jobsHtml + booksHtml) || '<li class="empty">No books staged yet.</li>';
  const cs = $('#convStatus');
  if (cs) cs.innerHTML = j.converter
    ? '<span style="color:var(--ok)">✓ conversion ready</span>'
    : '<span style="color:var(--muted)">— install Calibre to convert PDF/MOBI</span>';

  $('#homeThumb').innerHTML = j.home
    ? `<div class="thumb"><img src="/dl/home/${encodeURIComponent(j.home.name)}?t=${Date.now()}"/>`+
      `<div><div class="nm">${esc(j.home.name)}</div>`+
      `<button class="x" style="font-size:.85rem" onclick="delHome()">✕ remove</button></div></div>`
    : '<div class="empty" style="margin-top:12px">No home-screen image set.</div>';
}

async function upload(url, files) {
  for (const f of files) {
    await fetch(url, { method:'POST', body:f, headers:{'X-Filename':encodeURIComponent(f.name)} });
  }
  refresh();
}
async function delBook(n){ await fetch('/api/books/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:decodeURIComponent(n)})}); refresh(); }
async function delHome(){ await fetch('/api/home/delete',{method:'POST'}); refresh(); }

function wire(dropId, pickId, url, accept) {
  const drop=$(dropId), pick=$(pickId);
  ['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('hover');}));
  ['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('hover');}));
  drop.addEventListener('drop', ev=>upload(url, ev.dataTransfer.files));
  pick.addEventListener('change', ()=>{ upload(url, pick.files); pick.value=''; });
}
wire('#dropBooks','#pickBooks','/api/books');
wire('#dropHome','#pickHome','/api/home');

$('#readerHost').addEventListener('change', e =>
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reader_host:e.target.value})}));

$('#pushBtn').addEventListener('click', async () => {
  const btn=$('#pushBtn'), msg=$('#pushMsg');
  btn.disabled=true; msg.textContent='Sending…';
  try { const r = await (await fetch('/api/push',{method:'POST'})).json();
        msg.textContent = r.message; }
  catch(e){ msg.textContent='Failed: '+e; }
  btn.disabled=false; refresh();
});

refresh(); setInterval(refresh, 2000);
</script></body></html>
"""


# ---- HTTP handler -----------------------------------------------------------


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_file(self, folder, name):
            safe = safe_name(urllib.parse.unquote(name))
            full = os.path.join(folder, safe) if safe else None
            if not full or not os.path.isfile(full) or os.path.dirname(os.path.abspath(full)) != os.path.abspath(folder):
                self.send_error(404); return
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(os.path.getsize(full)))
            self.end_headers()
            with open(full, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 65536)

        def _read_body(self):
            n = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/manifest":
                host = self.headers.get("Host") or ("%s:%d" % (lan_ip(), self.server.server_address[1]))
                self._json(200, build_manifest(host))
            elif path == "/api/state":
                reader_host = cfg.get("reader_host", DEFAULT_READER_HOST)
                self._json(200, {
                    "books": list_books(),
                    "home": current_home(),
                    "reader_host": reader_host,
                    "reader_online": reader_online(reader_host),
                    "sync_url": "http://%s:%d/api/manifest" % (lan_ip(), self.server.server_address[1]),
                    "jobs": jobs_snapshot(),
                    "converter": find_ebook_convert() is not None,
                })
            elif path.startswith("/dl/books/"):
                self._serve_file(BOOKS_DIR, path[len("/dl/books/"):])
            elif path.startswith("/dl/home/"):
                self._serve_file(HOME_DIR, path[len("/dl/home/"):])
            else:
                self.send_error(404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/books":
                name = safe_name(urllib.parse.unquote(self.headers.get("X-Filename", "")))
                data = self._read_body()
                if not name:
                    self._json(400, {"ok": False, "message": "bad filename"}); return
                ext = os.path.splitext(name)[1].lower()
                if ext in BOOK_EXT:
                    with open(os.path.join(BOOKS_DIR, name), "wb") as f:
                        f.write(data)
                    self._json(200, {"ok": True}); return
                if ext in CONVERTIBLE_EXT:
                    if find_ebook_convert() is None:
                        self._json(400, {"ok": False,
                                         "message": "Install Calibre to convert " + ext + " files"}); return
                    os.makedirs(WORK_DIR, exist_ok=True)
                    tmp = os.path.join(WORK_DIR, uuid.uuid4().hex + "_" + name)
                    with open(tmp, "wb") as f:
                        f.write(data)
                    jid = add_job(name)
                    threading.Thread(target=convert_worker, args=(tmp, os.path.splitext(name)[0], jid),
                                     daemon=True).start()
                    self._json(200, {"ok": True, "job": jid}); return
                self._json(400, {"ok": False, "message": "unsupported type: " + ext}); return
            elif path == "/api/home":
                name = safe_name(urllib.parse.unquote(self.headers.get("X-Filename", "")))
                data = self._read_body()
                if not name or os.path.splitext(name)[1].lower() not in IMAGE_EXT:
                    self._json(400, {"ok": False, "message": "not an image"}); return
                for old in os.listdir(HOME_DIR):        # keep only the latest image
                    try: os.remove(os.path.join(HOME_DIR, old))
                    except OSError: pass
                with open(os.path.join(HOME_DIR, name), "wb") as f:
                    f.write(data)
                self._json(200, {"ok": True})
            elif path == "/api/books/delete":
                name = safe_name((json.loads(self._read_body() or b"{}")).get("name", ""))
                if name and os.path.isfile(os.path.join(BOOKS_DIR, name)):
                    os.remove(os.path.join(BOOKS_DIR, name))
                self._json(200, {"ok": True})
            elif path == "/api/home/delete":
                for old in os.listdir(HOME_DIR):
                    try: os.remove(os.path.join(HOME_DIR, old))
                    except OSError: pass
                self._json(200, {"ok": True})
            elif path == "/api/config":
                body = json.loads(self._read_body() or b"{}")
                if "reader_host" in body:
                    cfg["reader_host"] = str(body["reader_host"]).strip() or DEFAULT_READER_HOST
                    save_config(cfg)
                self._json(200, {"ok": True})
            elif path == "/api/push":
                reader_host = cfg.get("reader_host", DEFAULT_READER_HOST)
                books = list_books()
                if not books:
                    self._json(200, {"ok": True, "message": "No books staged."}); return
                sent, failed = 0, 0
                first_err = ""
                for b in books:
                    try:
                        with open(os.path.join(BOOKS_DIR, b["name"]), "rb") as f:
                            status, _ = push_book_to_reader(reader_host, b["name"], f.read())
                        if status == 200: sent += 1
                        else: failed += 1
                    except Exception as e:
                        failed += 1
                        first_err = first_err or str(e)
                msg = "Sent %d book(s)" % sent + (", %d failed (%s)" % (failed, first_err) if failed else ".")
                self._json(200, {"ok": failed == 0, "message": msg})
            else:
                self.send_error(404)

    return Handler


# ---- Main -------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="CrossPoint Reader dashboard + staging server.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--reader-host", default=None, help="override reader host for 'Send now'")
    args = ap.parse_args()

    ensure_dirs()
    cfg = load_config()
    if args.reader_host:
        cfg["reader_host"] = args.reader_host
        save_config(cfg)

    ip = lan_ip()
    print("CrossPoint Reader Dashboard")
    print("  Open the dashboard:  http://localhost:%d" % args.port)
    print("  Staging folder:      %s" % BASE)
    print("  Reader sync address: http://%s:%d   (enter this in the reader's Sync setting)" % (ip, args.port))
    print()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(cfg))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        server.shutdown()


if __name__ == "__main__":
    main()
