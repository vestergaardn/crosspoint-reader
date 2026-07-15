# CrossPoint sync-on-wake protocol

The contract between the Mac **dashboard/staging server** (`crosspoint_dashboard.py`)
and the reader's firmware **sync-on-wake** feature. Both sides implement this; keep them
in step.

## Transport

Plain HTTP on the local network. The reader is configured with the Mac's address, e.g.
`http://192.168.1.50:8765`. `HttpDownloader` already supports plain HTTP for local servers
(`src/network/HttpDownloader.h`).

## Manifest — `GET <server>/api/manifest`

Returns JSON describing everything that should be on the reader:

```json
{
  "version": 1,
  "books": [
    {
      "name": "My Book.epub",
      "url":  "http://192.168.1.50:8765/dl/books/My%20Book.epub",
      "size": 402118,
      "sha1": "8f1c...e9"
    }
  ],
  "home": {
    "name": "cover.png",
    "url":  "http://192.168.1.50:8765/dl/home/cover.png",
    "sha1": "2ab9...4c"
  }
}
```

- `url` fields are absolute and built from the `Host` header of the reader's request, so
  they always point back at whatever address the reader used — no server-side IP config.
- `home` is `null` when no home image is staged.
- `sha1` is the content hash; it is how the reader decides what is new.

## Downloads

- `GET /dl/books/<name>` → the EPUB bytes.
- `GET /dl/home/<name>`  → the image bytes (png/jpg/bmp).

## Firmware behavior (what "sync on wake" does)

On boot, if `syncOnWake` is enabled AND WiFi credentials exist (and, if
`syncOnlyWhenCharging` is set, `gpio.isUsbConnected()` is true):

1. Connect to the saved WiFi network. On failure, abort quietly and continue booting.
2. `GET <syncServerUrl>/api/manifest` (small body → `HttpDownloader::fetchUrl`).
   On any error, abort quietly and continue.
3. Parse with ArduinoJson.
4. **Books:** for each entry whose `sha1` is not in the synced-state store,
   `HttpDownloader::downloadToFile(url, "/<name>")` to the SD root, then record its `sha1`.
   Skip entries already recorded (idempotent — no re-downloads).
5. **Home image:** if `home` is non-null and its `sha1` differs from the stored one,
   download to a temp path, `installSleepImage(tempPath, maxDim)` → `/sleep.bmp`
   (converts png/jpg→grayscale bmp), set the sleep-screen mode to CUSTOM, save settings,
   store the new `sha1`.
6. Disconnect WiFi / turn the radio off (battery).
7. Continue to the home screen.

Everything is best-effort: any network or parse failure just means "try again next boot".
The `sha1` records make it safe to run on every boot without re-downloading.

## Settings added

- `syncOnWake` (bool, default false)
- `syncServerUrl` (string, e.g. `http://192.168.1.50:8765`)
- `syncOnlyWhenCharging` (bool, default true — protects battery)

## Persisted sync state

A small `PersistableStore` (like `WifiCredentialStore`) holding:
- the set of book `sha1`s already downloaded,
- the last home-image `sha1`.

## Notes / trade-offs

- The Mac must be awake, on the same WiFi, and running the dashboard for a sync to happen;
  otherwise the reader just tries again next boot.
- DHCP can change the Mac's IP. Set a DHCP reservation, or we can add mDNS discovery later.
- Syncing adds a few seconds to boot and uses radio power — hence the "only when charging"
  default and the on/off toggle.
