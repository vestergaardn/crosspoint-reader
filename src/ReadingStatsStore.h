#pragma once
#include <ArduinoJson.h>
#include <PersistableStore.h>

#include <cstdint>

/**
 * @brief Lifetime cumulative reading statistics, persisted to SD as JSON.
 *
 * Tracks a single stat: total forward page turns across every book ("pages read").
 * Deliberately clock-free — the ESP32-C3 X4 has no RTC and fully powers off on
 * battery sleep, so anything date-based (daily streaks) is impossible offline.
 * A monotonic counter needs no wall clock: it just grows forever and survives
 * power-off because it lives on the SD card.
 */
class ReadingStatsStore : public PersistableStore<ReadingStatsStore> {
 private:
  uint32_t totalPagesRead = 0;
  bool dirty = false;  // set by addPage(); not persisted. Guards writes (see flush()).

  ReadingStatsStore() = default;
  ~ReadingStatsStore() = default;

  friend class PersistableStore<ReadingStatsStore>;

 public:
  static const char* getFilePath() { return "/.crosspoint/reading_stats.json"; }
  void toJson(JsonDocument& doc) const;
  bool fromJson(JsonVariantConst doc);

  uint32_t getTotalPagesRead() const { return totalPagesRead; }

  // Count one forward page turn. Kept in RAM (cheap, UI-thread only); persisted
  // later by flush() at reader exit — never writes the SD card per page turn
  // (SPIFFS/SD wear, CLAUDE.md rule #8).
  void addPage() {
    if (totalPagesRead < UINT32_MAX) totalPagesRead++;
    dirty = true;
  }

  // Persist only if pages were counted since the last save (value-change guard).
  // Call from a reader's onExit(): that fires once per session and also on the
  // way into deep sleep, so it's the natural low-frequency flush point.
  void flush() {
    if (!dirty) return;
    if (saveToFile()) dirty = false;
  }
};

#define READING_STATS ReadingStatsStore::getInstance()
