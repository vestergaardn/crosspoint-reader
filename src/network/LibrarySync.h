#pragma once

class GfxRenderer;

/**
 * Sync-on-wake: pull new books and a home-screen image from a server running on
 * the user's computer (the CrossPoint dashboard) whenever the reader boots.
 *
 * The reader is the client here — it connects to WiFi, reads a small JSON manifest
 * from SETTINGS.syncServerUrl, downloads anything it does not already have to the SD
 * card, updates the sleep image if it changed, then turns WiFi back off.
 *
 * See tools/reader-sync/SYNC-PROTOCOL.md for the manifest format.
 */
namespace LibrarySync {

// Runs one best-effort sync. Blocking — intended to be called from setup() before
// the main loop starts, guarded by the SETTINGS.syncOnWake / charging conditions.
// Draws a simple status screen via `renderer`. Any failure (no WiFi, no server,
// bad manifest) is logged and swallowed so boot always continues.
void runAtBoot(GfxRenderer& renderer);

}  // namespace LibrarySync
