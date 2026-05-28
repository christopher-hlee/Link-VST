#pragma once
#include "IPlug_include_in_plug_hdr.h"
#include "ApiClient.h"
#include "PreviewPlayer.h"
#include <vector>
#include <string>
#include <mutex>

struct GenerateSettings {
    int         count             = 4;
    std::string phrase_type;       // "" = any (use taste profile)
    std::string key;
    std::string mode;
    int         bars              = 4;
    std::string hint;
    // Humanization
    float       swing             = 0.f;
    int         velocity_variance = 0;
    float       timing_variance   = 0.f;
};

enum class PluginState { Idle, Uploading, Generating, Ready };

class LinkVST final : public iplug::Plugin {
public:
    LinkVST(const iplug::InstanceInfo& info);

    void OnUIOpen() override;
    void OnUIClose() override;
    void ProcessBlock(iplug::sample** inputs, iplug::sample** outputs, int nFrames) override;

    // ── Generate ──────────────────────────────────────────────────────────────
    void RequestGenerate();   // uses mSettings
    void UploadMidiFile(const std::string& path);

    // ── Settings from UI dropdowns / sliders ──────────────────────────────────
    void SetSetting(const std::string& key, const std::string& value);
    void SetHumanize(const std::string& param, float value);

    // ── Library ───────────────────────────────────────────────────────────────
    void DeletePhrase(int id);
    void RefreshLibrary();

    // ── Drag-out ──────────────────────────────────────────────────────────────
    void BeginDragOut(int phrase_index, float x, float y);
    void BeginLibraryDragOut(int library_index, float x, float y);

    // ── Preview ───────────────────────────────────────────────────────────────
    void TogglePreview(int library_id);
    bool IsPreviewPlaying(int library_id) const {
        return mPreviewId == library_id && mPreview.IsPlaying();
    }
    void StopPreview() { mPreview.Stop(); mPreviewId = -1; }

    // ── Accessors ─────────────────────────────────────────────────────────────
    const std::vector<PhraseInfo>& GetGeneratedPhrases() const { return mGeneratedPhrases; }
    const std::vector<PhraseInfo>& GetLibraryPhrases()   const { return mLibraryPhrases; }
    PluginState GetState()         const { return mState; }
    std::string GetStatusMessage() const { return mStatusMessage; }

private:
    ApiClient       mApi;
    PreviewPlayer   mPreview;
    int             mPreviewId = -1;
    GenerateSettings mSettings;

    std::vector<PhraseInfo> mGeneratedPhrases;
    std::vector<PhraseInfo> mLibraryPhrases;
    PluginState             mState = PluginState::Idle;
    std::string             mStatusMessage;
    mutable std::mutex      mMutex;

    void SetState(PluginState state, const std::string& msg = "");

    void DoDragOut(const std::vector<uint8_t>& midi_bytes, const std::string& filename,
                   void* platform_view, float x, float y);
};
