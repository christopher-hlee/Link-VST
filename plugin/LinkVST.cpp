#include "LinkVST.h"
#include "IPlug_include_in_plug_src.h"
#include <fstream>
#include <filesystem>

// Config — override with environment or build define
#ifndef LINKVST_API_URL
#define LINKVST_API_URL "http://localhost:8002"
#endif

#ifndef LINKVST_API_KEY
#define LINKVST_API_KEY ""
#endif

LinkVST::LinkVST(const iplug::InstanceInfo& info)
  : iplug::Plugin(info, iplug::MakeConfig(0, 1))
  , mApi(LINKVST_API_URL, LINKVST_API_KEY)
{
  // Load library on startup
  RefreshLibrary();
}

void LinkVST::OnUIOpen() {
  RefreshLibrary();
}

void LinkVST::OnUIClose() {}

bool LinkVST::OnKeyDown(const iplug::IKeyPress& key) {
  return false;
}

void LinkVST::RequestGenerate(int count, const std::string& phrase_type,
                               const std::string& key, const std::string& mode,
                               int bars, const std::string& hint) {
  SetState(PluginState::Generating, "Generating with Claude...");

  mApi.Generate(count, phrase_type, key, mode, bars, hint, true,
    [this](bool ok, std::vector<PhraseInfo> phrases, std::string err) {
      std::lock_guard<std::mutex> lock(mMutex);
      if (ok) {
        mGeneratedPhrases = std::move(phrases);
        SetState(PluginState::Ready,
          std::to_string(mGeneratedPhrases.size()) + " phrases ready — drag to DAW");
      } else {
        SetState(PluginState::Idle, "Error: " + err);
      }
    });
}

void LinkVST::UploadMidiFile(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return;
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(f)), {});
  std::string filename = std::filesystem::path(path).filename().string();

  SetState(PluginState::Uploading, "Uploading " + filename + "...");

  mApi.UploadMidi(filename, bytes,
    [this, filename](bool ok, std::string msg, std::string err) {
      if (ok) {
        SetState(PluginState::Idle, "Uploaded: " + filename + ". Taste profile updated.");
      } else {
        SetState(PluginState::Idle, "Upload error: " + err);
      }
    });
}

void LinkVST::SavePhrase(int index) {
  // Phrases are inserted into library server-side on generation.
  // This is a no-op stub; future: mark as "saved" vs "unsaved" locally.
  RefreshLibrary();
}

void LinkVST::DeletePhrase(int id) {
  mApi.DeleteLibraryItem(id);
  RefreshLibrary();
}

void LinkVST::RefreshLibrary() {
  auto items = mApi.GetLibrary();
  std::lock_guard<std::mutex> lock(mMutex);
  mLibraryPhrases = std::move(items);
}

void LinkVST::BeginDragOut(int phrase_index, float x, float y) {
  if (phrase_index < 0 || phrase_index >= (int)mGeneratedPhrases.size()) return;
  const auto& phrase = mGeneratedPhrases[phrase_index];
  std::string filename = phrase.key + "_" + phrase.mode + "_" +
                         phrase.phrase_type + ".mid";
  void* view = GetUI() ? GetUI()->GetPlatformContext() : nullptr;
  DoDragOut(phrase.midi_bytes, filename, view, x, y);
}

void LinkVST::SetState(PluginState state, const std::string& msg) {
  mState = state;
  mStatusMessage = msg;
  if (GetUI()) GetUI()->SetAllControlsDirty();
}
