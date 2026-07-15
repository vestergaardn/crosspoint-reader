# CrossPoint Reader Sync — Mac tools

Get **EPUB** books (and a home-screen image) onto your reader over WiFi, no cables. Two
tools here, plus the device-side firmware that makes "grab it on wake" work.

> The reader opens **EPUB** (and `.xtc/.txt/.md/.bmp`), **not PDF**. Convert a PDF to EPUB
> first (e.g. with [Calibre](https://calibre-ebook.com)), then send the EPUB.

## 1. `crosspoint_dashboard.py` — the control panel (recommended)

A small website + staging server on your Mac. Run it, open <http://localhost:8765>, and:

- **Books** — drop EPUBs; they stage in `~/CrossPointReader/books/`.
- **Home screen** — drop an image; staged in `~/CrossPointReader/home/`.
- **Send now** — optionally push books immediately to a reader that's awake on its
  wireless-transfer screen.

It also publishes a manifest (`/api/manifest`) and serves the files, so the reader can
**pull new items by itself on wake** (see firmware below).

```bash
python3 crosspoint_dashboard.py          # then open http://localhost:8765
```

The dashboard shows the **sync address** (your Mac's LAN IP, e.g. `http://192.168.1.50:8765`)
to enter on the reader.

## 2. Sync-on-wake (firmware) — hands-off

With the firmware feature enabled, the reader connects to WiFi at boot, reads the dashboard's
manifest, downloads anything new (books → library, image → sleep screen), and disconnects —
no button presses. Drop files on the Mac any time; they appear on the reader next time it
wakes (while charging, by default).

- Setup + build + test: **[FIRMWARE-NOTES.md](FIRMWARE-NOTES.md)**
- Wire protocol both sides implement: **[SYNC-PROTOCOL.md](SYNC-PROTOCOL.md)**

Enable on the reader: Settings → System → **Sync books on wake**; set **Library Sync Server
URL** (the address the dashboard shows) on the reader's web Settings page.

## 3. `reader_sync.py` — the simple push-only tool

The original minimal version: a watched folder (`~/Drop-to-Reader`) + a basic drag-drop page
that push straight to an awake reader's `/upload`. No staging, no manifest. Use the dashboard
instead unless you want something tiny.

```bash
python3 reader_sync.py
```

## Requirements

Python 3 (ships with macOS). No `pip` packages. The dashboard listens on `0.0.0.0` so the
reader can reach it — use on a trusted home network.
