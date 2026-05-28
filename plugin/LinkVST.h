#pragma once
#include "IPlug_include_in_plug_hdr.h"
#include "ApiClient.h"
#include "PreviewPlayer.h"
#include <vector>
#include <string>
#include <mutex>
#include <set>

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
    void ProcessBlock(iplug::sample** inputs, iplug::sample** outputs, int nFrames) override;

    // ── Generate ──────────────────────────────────────────────────────────────
    void RequestGenerate(int count = 4, const std::string& phrase_type = "",
                         const std::string& key = "", const std::string& mode = "",
                         int bars = 4, const std::string& hint = "");

    // ── Upload ────────────────────────────────────────────────────────────────
    void UploadMidiFile(const std::string& path);

    // ── Library ───────────────────────────────────────────────────────────────
    void SavePhrase(int index);
    void DeletePhrase(int id);
    void RefreshLibrary();

    // ── Drag-out ──────────────────────────────────────────────────────────────
    // From generated phrase tiles
    void BeginDragOut(int phrase_index, float x, float y);
    // From library browser rows
    void BeginLibraryDragOut(int library_index, float x, float y);

    // ── Preview ───────────────────────────────────────────────────────────────
    void TogglePreview(int library_id);
    bool IsPreviewPlaying(int library_id) const { return mPreviewId == library_id && mPreview.IsPlaying(); }
    void StopPreview() { mPreview.Stop(); mPreviewId = -1; }

    // ── Accessors for panels ──────────────────────────────────────────────────
    const std::vector<PhraseInfo>& GetGeneratedPhrases() const { return mGeneratedPhrases; }
    const std::vector<PhraseInfo>& GetLibraryPhrases()   const { return mLibraryPhrases; }
    PluginState GetState()         const { return mState; }
    std::string GetStatusMessage() const { return mStatusMessage; }

private:
    ApiClient     mApi;
    PreviewPlayer mPreview;
    int           mPreviewId = -1;

    std::vector<PhraseInfo> mGeneratedPhrases;
    std::vector<PhraseInfo> mLibraryPhrases;
    PluginState             mState = PluginState::Idle;
    std::string             mStatusMessage;
    mutable std::mutex      mMutex;

    void SetState(PluginState state, const std::string& msg = "");

    // Platform drag-out — implemented in DragOut_mac.mm / DragOut_win.cpp
    void DoDragOut(const std::vector<uint8_t>& midi_bytes, const std::string& filename,
                   void* platform_view, float x, float y);
};
