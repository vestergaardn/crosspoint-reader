#include "SyncStateStore.h"

void SyncStateStore::toJson(JsonDocument& doc) const {
  JsonArray arr = doc["books"].to<JsonArray>();
  for (const auto& h : bookHashes) arr.add(h);
  doc["home"] = homeHash;
}

bool SyncStateStore::fromJson(JsonVariantConst doc) {
  bookHashes.clear();
  JsonArrayConst arr = doc["books"];
  if (!arr.isNull()) {
    bookHashes.reserve(arr.size());
    for (JsonVariantConst v : arr) {
      const char* s = v | "";  // const char* fallback — never | std::string("") (flash bloat)
      if (s && s[0]) bookHashes.emplace_back(s);
      if (bookHashes.size() >= MAX_TRACKED_BOOKS) break;
    }
  }
  homeHash = doc["home"] | "";
  return true;
}

bool SyncStateStore::hasBook(const std::string& sha1) const {
  for (const auto& h : bookHashes) {
    if (h == sha1) return true;
  }
  return false;
}

void SyncStateStore::addBook(const std::string& sha1) {
  if (sha1.empty() || hasBook(sha1)) return;
  // Drop the oldest entry once the cap is hit so the file/RAM stays bounded.
  if (bookHashes.size() >= MAX_TRACKED_BOOKS) bookHashes.erase(bookHashes.begin());
  bookHashes.push_back(sha1);
}

void SyncStateStore::releaseMemory() {
  bookHashes.clear();
  bookHashes.shrink_to_fit();
}
