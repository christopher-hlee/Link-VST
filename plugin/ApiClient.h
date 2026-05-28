#pragma once
#include <string>
#include <functional>
#include <vector>

struct PhraseInfo {
  int         id = -1;              // library ID (for preview + delete)
  std::string phrase_type;
  std::string key;
  std::string mode;
  int         tempo_bpm = 120;
  int         bars      = 4;
  std::string description;
  std::vector<uint8_t> midi_bytes;  // decoded from base64 (empty for library items)
};

using GenerateCallback = std::function<void(bool success, std::vector<PhraseInfo> phrases, std::string error)>;
using UploadCallback   = std::function<void(bool success, std::string message, std::string error)>;

class ApiClient {
public:
  ApiClient(std::string base_url, std::string api_key);

  // POST /api/upload-midi — async
  void UploadMidi(const std::string& filename,
                  const std::vector<uint8_t>& midi_bytes,
                  UploadCallback callback);

  // POST /api/generate — async
  void Generate(int count, const std::string& phrase_type,
                const std::string& key, const std::string& mode,
                int bars, const std::string& hint, bool variety,
                float swing, int velocity_variance, float timing_variance,
                GenerateCallback callback);

  // GET /api/library — synchronous for simplicity
  std::vector<PhraseInfo> GetLibrary();

  // GET /api/library/{id}/midi — fetch MIDI bytes
  std::vector<uint8_t> GetMidi(int id);

  // DELETE /api/library/{id}
  bool DeleteLibraryItem(int id);

private:
  std::string mBaseUrl;
  std::string mApiKey;

  std::string DoRequest(const std::string& method,
                        const std::string& path,
                        const std::string& body,
                        const std::string& content_type = "application/json");

  static std::vector<uint8_t> Base64Decode(const std::string& encoded);
};
