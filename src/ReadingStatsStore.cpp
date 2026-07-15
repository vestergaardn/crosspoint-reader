#include "ReadingStatsStore.h"

#include <Logging.h>

void ReadingStatsStore::toJson(JsonDocument& doc) const { doc["totalPagesRead"] = totalPagesRead; }

bool ReadingStatsStore::fromJson(JsonVariantConst doc) {
  totalPagesRead = doc["totalPagesRead"] | static_cast<uint32_t>(0);
  dirty = false;
  LOG_DBG("RST", "Reading stats loaded: %lu pages", static_cast<unsigned long>(totalPagesRead));
  return true;
}
