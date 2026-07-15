#include "SleepImageUtil.h"

#include <FsHelpers.h>
#include <HalStorage.h>
#include <JpegToBmpConverter.h>
#include <Logging.h>
#include <Memory.h>
#include <PngToBmpConverter.h>

namespace {
constexpr const char* SLEEP_BMP_PATH = "/sleep.bmp";
constexpr size_t COPY_BUFFER_SIZE = 2048;
}  // namespace

bool installSleepImage(const std::string& srcPath, int maxDim) {
  HalFile in;
  if (!Storage.openFileForRead("SLEEP", srcPath, in)) {
    LOG_ERR("SLEEP", "Failed to open source image: %s", srcPath.c_str());
    return false;
  }

  HalFile out;
  if (!Storage.openFileForWrite("SLEEP", SLEEP_BMP_PATH, out)) {
    LOG_ERR("SLEEP", "Failed to open %s for write", SLEEP_BMP_PATH);
    return false;
  }

  bool success = false;
  if (FsHelpers::hasBmpExtension(srcPath)) {
    // Copy the BMP verbatim (no resize). Heap buffer keeps this off the small task stack.
    auto buffer = makeUniqueNoThrow<char[]>(COPY_BUFFER_SIZE);
    if (!buffer) {
      LOG_ERR("SLEEP", "OOM: %u byte copy buffer", static_cast<unsigned>(COPY_BUFFER_SIZE));
    } else {
      success = true;
      int bytesRead;
      while ((bytesRead = in.read(buffer.get(), COPY_BUFFER_SIZE)) > 0) {
        if (out.write(buffer.get(), bytesRead) != bytesRead) {
          success = false;
          break;
        }
      }
    }
  } else if (FsHelpers::hasJpgExtension(srcPath)) {
    // HalFile is a Print, so the write handle passes straight in as the BMP output stream.
    success = JpegToBmpConverter::jpegFileToBmpStreamWithSize(in, out, maxDim, maxDim);
  } else if (FsHelpers::hasPngExtension(srcPath)) {
    success = PngToBmpConverter::pngFileToBmpStreamWithSize(in, out, maxDim, maxDim);
  } else {
    LOG_ERR("SLEEP", "Unsupported sleep image type: %s", srcPath.c_str());
  }

  // Close the output before any remove() (close-before-remove rule).
  out.close();
  in.close();

  if (!success) {
    LOG_ERR("SLEEP", "Failed to install sleep image from %s", srcPath.c_str());
    Storage.remove(SLEEP_BMP_PATH);
  }
  return success;
}
