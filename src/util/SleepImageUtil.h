#pragma once

#include <string>

// Installs a sleep-screen image at /sleep.bmp from the given source file.
// - .bmp sources are copied verbatim (no resize), matching the existing "Set Cover" behaviour.
// - .jpg/.jpeg and .png sources are converted to a grayscale BMP bounded to maxDim x maxDim
//   (aspect preserved), reusing the same converters as book covers. Output streams straight to
//   SD, so the decoded image is never held in RAM.
//
// Returns false on failure (and removes any partial /sleep.bmp). Does NOT modify settings — the
// caller is responsible for setting SLEEP_SCREEN_MODE::CUSTOM and saving.
bool installSleepImage(const std::string& srcPath, int maxDim);
