#pragma once
#include <ArduinoJson.h>
#include <PersistableStore.h>

#include <string>
#include <vector>

/**
 * Remembers which manifest items sync-on-wake has already downloaded, so a
 * repeated boot does not re-download the same books. Keyed by content hash
 * (sha1 from the manifest), which also means a changed file (new hash) is
 * treated as new and re-fetched.
 *
 * Persisted as /.crosspoint/sync.json via the PersistableStore base.
 */
class SyncStateStore : public PersistableStore<SyncStateStore> {
 private:
  std::vector<std::string> bookHashes;
  std::string homeHash;

  static constexpr size_t MAX_TRACKED_BOOKS = 512;

  SyncStateStore() = default;
  friend class PersistableStore<SyncStateStore>;

 public:
  static const char* getFilePath() { return "/.crosspoint/sync.json"; }
  void toJson(JsonDocument& doc) const;
  bool fromJson(JsonVariantConst doc);

  bool hasBook(const std::string& sha1) const;
  void addBook(const std::string& sha1);

  const std::string& getHomeHash() const { return homeHash; }
  void setHomeHash(const std::string& sha1) { homeHash = sha1; }

  // Free the book-hash vector once the boot sync is done. The list is only needed
  // while syncing, so it should not stay resident (RAM) during reading.
  void releaseMemory();
};

// Helper macro to access the sync-state store
#define SYNC_STATE SyncStateStore::getInstance()
