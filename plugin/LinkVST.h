#pragma once
#include "IPlug_include_in_plug_hdr.h"
#include "ApiClient.h"
#include <vector>
#include <string>
#include <mutex>

// Panel forward declarations
class GeneratePanel;
class LibraryPanel;

enum class PluginState {
  Idle,
  Uploading,
  Generating,
  Ready,
};

class LinkVST final : public iplug::Plugin {
public:
  LinkVST(const iplug::InstanceInfo& info);

  // iPlug2 overrides
  void OnUIOpen() override;
  void OnUIClose() override;
  bool OnKeyDown(const iplug::IKeyPress& key) override;

  // Called from UI panels
  void RequestGenerate(int count = 4, const std::string& phrase_type = "",
                       const std::string& key = "", const std::string& mode = "",
                       int bars = 4, const std::string& hint = "");

  void UploadMidiFile(const std::string& path);

  void SavePhrase(int index);
  void DeletePhrase(int id);

  // Drag-out: called when user starts drag on a phrase tile
  void BeginDragOut(int phrase_index);

  const std::vector<PhraseInfo>& GetGeneratedPhrases() const { return mGeneratedPhrases; }
  const std::vector<PhraseInfo>& GetLibraryPhrases()   const { return mLibraryPhrases; }
  PluginState GetState() const { return mState; }
  std::string GetStatusMessage() const { return mStatusMessage; }

private:
  ApiClient mApi;
  std::vector<PhraseInfo> mGeneratedPhrases;
  std::vector<PhraseInfo> mLibraryPhrases;
  PluginState mState = PluginState::Idle;
  std::string mStatusMessage;
  mutable std::mutex mMutex;

  void SetState(PluginState state, const std::string& msg = "");
  void RefreshLibrary();

  // Platform drag-out implementation
  void DoDragOut(const std::vector<uint8_t>& midi_bytes, const std::string& filename);
};
