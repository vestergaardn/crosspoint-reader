#include "network/LibrarySync.h"

#include <ArduinoJson.h>
#include <GfxRenderer.h>
#include <HalStorage.h>
#include <I18n.h>
#include <Logging.h>
#include <WiFi.h>
#include <esp_task_wdt.h>

#include <algorithm>
#include <cstring>
#include <string>

#include "CrossPointSettings.h"
#include "SyncStateStore.h"
#include "WifiCredentialStore.h"
#include "fontIds.h"
#include "network/HttpDownloader.h"
#include "util/BookCacheUtils.h"
#include "util/SleepImageUtil.h"

namespace {

constexpr unsigned long WIFI_CONNECT_TIMEOUT_MS = 15000;
constexpr const char* SLEEP_TMP_PATH = "/.crosspoint/sleep_sync.tmp";

void drawStatus(GfxRenderer& renderer, const char* msg) {
  renderer.clearScreen();
  renderer.drawCenteredText(UI_10_FONT_ID, renderer.getScreenHeight() / 2, msg, true, EpdFontFamily::BOLD);
  renderer.displayBuffer();
}

// Connect to the last-used saved WiFi network without any UI. Returns true on
// success. Mirrors WifiSelectionActivity::attemptConnection() but headless.
bool connectWifiHeadless() {
  WIFI_STORE.loadFromFile();
  const std::string ssid = WIFI_STORE.getLastConnectedSsid();
  if (ssid.empty()) {
    LOG_INF("SYNC", "No last-connected WiFi network saved");
    return false;
  }
  const WifiCredential* cred = WIFI_STORE.findCredential(ssid);
  if (!cred) {
    LOG_INF("SYNC", "No stored credential for '%s'", ssid.c_str());
    return false;
  }

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);
  delay(100);
  if (cred->password.empty()) {
    WiFi.begin(cred->ssid.c_str());
  } else {
    WiFi.begin(cred->ssid.c_str(), cred->password.c_str());
  }

  const unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT_MS) {
    esp_task_wdt_reset();  // long boot-time blocking loop: keep the task watchdog fed
    delay(100);
  }
  return WiFi.status() == WL_CONNECTED;
}

void teardownWifi() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
}

std::string trimTrailingSlash(const char* s) {
  std::string url(s);
  while (!url.empty() && url.back() == '/') url.pop_back();
  return url;
}

// Progress callback: only job is to keep the watchdog alive during a large download.
void onDownloadProgress(size_t, size_t) { esp_task_wdt_reset(); }

// Download books listed in the manifest that we haven't already fetched.
int syncBooks(JsonArrayConst books) {
  int downloaded = 0;
  for (JsonObjectConst b : books) {
    const char* name = b["name"] | "";
    const char* url = b["url"] | "";
    const char* sha1 = b["sha1"] | "";
    if (!name[0] || !url[0] || !sha1[0]) continue;
    if (std::strchr(name, '/')) continue;  // manifest names are plain filenames
    if (SYNC_STATE.hasBook(sha1)) continue;

    const std::string dest = std::string("/") + name;
    const auto result = HttpDownloader::downloadToFile(url, dest, onDownloadProgress);
    if (result == HttpDownloader::OK) {
      clearBookCache(dest);  // drop stale .crosspoint cache for an overwritten book
      SYNC_STATE.addBook(sha1);
      downloaded++;
      LOG_INF("SYNC", "Downloaded '%s'", name);
    } else {
      LOG_ERR("SYNC", "Download failed (%d): '%s'", static_cast<int>(result), name);
    }
  }
  return downloaded;
}

// Install a new home-screen image if the manifest's differs from the last one synced.
bool syncHomeImage(GfxRenderer& renderer, JsonObjectConst home) {
  if (home.isNull()) return false;
  const char* url = home["url"] | "";
  const char* sha1 = home["sha1"] | "";
  if (!url[0] || !sha1[0] || SYNC_STATE.getHomeHash() == sha1) return false;

  const auto result = HttpDownloader::downloadToFile(url, SLEEP_TMP_PATH, onDownloadProgress);
  if (result != HttpDownloader::OK) {
    LOG_ERR("SYNC", "Home image download failed (%d)", static_cast<int>(result));
    return false;
  }

  bool updated = false;
  const int maxDim = std::max(renderer.getScreenWidth(), renderer.getScreenHeight());
  if (installSleepImage(SLEEP_TMP_PATH, maxDim)) {
    SETTINGS.sleepScreen = CrossPointSettings::SLEEP_SCREEN_MODE::CUSTOM;
    SETTINGS.saveToFile();
    SYNC_STATE.setHomeHash(sha1);
    updated = true;
    LOG_INF("SYNC", "Installed new home-screen image");
  }
  Storage.remove(SLEEP_TMP_PATH);
  return updated;
}

}  // namespace

void LibrarySync::runAtBoot(GfxRenderer& renderer) {
  std::string manifestUrl = trimTrailingSlash(SETTINGS.syncServerUrl);
  if (manifestUrl.empty()) return;
  // The user may omit the scheme (fewer characters to type on-device) — default to https.
  if (manifestUrl.rfind("http://", 0) != 0 && manifestUrl.rfind("https://", 0) != 0) {
    manifestUrl = "https://" + manifestUrl;
  }

  drawStatus(renderer, tr(STR_SYNCING_LIBRARY));

  if (!connectWifiHeadless()) {
    LOG_INF("SYNC", "WiFi unavailable; skipping library sync");
    teardownWifi();
    return;
  }

  SYNC_STATE.loadFromFile();

  std::string body;
  if (!HttpDownloader::fetchUrl(manifestUrl, body)) {
    LOG_ERR("SYNC", "Could not fetch manifest from %s", manifestUrl.c_str());
    teardownWifi();
    return;
  }

  JsonDocument doc;
  const DeserializationError err = deserializeJson(doc, body);
  if (err) {
    LOG_ERR("SYNC", "Manifest parse error: %s", err.c_str());
    teardownWifi();
    return;
  }

  const int downloaded = syncBooks(doc["books"].as<JsonArrayConst>());
  const bool homeUpdated = syncHomeImage(renderer, doc["home"].as<JsonObjectConst>());

  if (downloaded > 0 || homeUpdated) SYNC_STATE.saveToFile();
  SYNC_STATE.releaseMemory();
  teardownWifi();

  LOG_INF("SYNC", "Library sync complete: %d new book(s), home %s", downloaded,
          homeUpdated ? "updated" : "unchanged");
}
