#include "LinkVST.h"
#include "IPlug_include_in_plug_src.h"
#include <fstream>
#include <filesystem>
#include <cstdlib>

#ifndef LINKVST_API_URL
#define LINKVST_API_URL "http://localhost:8002"
#endif
#ifndef LINKVST_API_KEY
#define LINKVST_API_KEY "linkvst-dev"
#endif

void BuildGenerateUI(iplug::igraphics::IGraphics*, LinkVST*);
void BuildLibraryPanel(iplug::igraphics::IGraphics*, LinkVST*,
                        const iplug::igraphics::IRECT&);

// ─── Constructor ─────────────────────────────────────────────────────────────

LinkVST::LinkVST(const iplug::InstanceInfo& info)
    : iplug::Plugin(info, iplug::MakeConfig(0, 1))
    , mApi(LINKVST_API_URL, LINKVST_API_KEY)
{
    SetChannelIO({iplug::IOConfig(0, 2)});
    RefreshLibrary();
}

// ─── UI ──────────────────────────────────────────────────────────────────────

void LinkVST::OnUIOpen() {
    auto* g = GetUI();
    if (!g) return;
    BuildGenerateUI(g, this);
    BuildLibraryPanel(g, this, iplug::igraphics::IRECT(0.f, 260.f, 600.f, 440.f));
    RefreshLibrary();
}

void LinkVST::OnUIClose() { StopPreview(); }

// ─── Audio ───────────────────────────────────────────────────────────────────

void LinkVST::ProcessBlock(iplug::sample** inputs, iplug::sample** outputs, int nFrames) {
    mPreview.Process(outputs, nFrames, GetSampleRate());
}

// ─── Settings ────────────────────────────────────────────────────────────────

void LinkVST::SetSetting(const std::string& key, const std::string& value) {
    if      (key == "phrase_type") mSettings.phrase_type = value;
    else if (key == "key")         mSettings.key         = value;
    else if (key == "mode")        mSettings.mode        = value;
    else if (key == "bars")        mSettings.bars        = std::atoi(value.c_str());
    else if (key == "count")       mSettings.count       = std::atoi(value.c_str());
    else if (key == "hint")        mSettings.hint        = value;
}

void LinkVST::SetHumanize(const std::string& param, float value) {
    if      (param == "swing")   mSettings.swing             = value;
    else if (param == "vel_var") mSettings.velocity_variance = (int)value;
    else if (param == "timing")  mSettings.timing_variance   = value;
}

// ─── Generate ────────────────────────────────────────────────────────────────

void LinkVST::RequestGenerate() {
    SetState(PluginState::Generating, "Generating with Claude…");

    auto s = mSettings;  // snapshot before async
    mApi.Generate(s.count, s.phrase_type, s.key, s.mode, s.bars, s.hint, true,
        s.swing, s.velocity_variance, s.timing_variance,
        [this](bool ok, std::vector<PhraseInfo> phrases, std::string err) {
            std::lock_guard<std::mutex> lock(mMutex);
            if (ok) {
                mGeneratedPhrases = std::move(phrases);
                SetState(PluginState::Ready,
                    std::to_string(mGeneratedPhrases.size()) + " phrases — drag to DAW");
            } else {
                SetState(PluginState::Idle, "Error: " + err);
            }
        });
}

// ─── Upload ──────────────────────────────────────────────────────────────────

void LinkVST::UploadMidiFile(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return;
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(f)), {});
    std::string filename = std::filesystem::path(path).filename().string();
    SetState(PluginState::Uploading, "Uploading " + filename + "…");
    mApi.UploadMidi(filename, bytes,
        [this, filename](bool ok, std::string, std::string err) {
            if (ok) SetState(PluginState::Idle, "Uploaded " + filename + " — profile updated.");
            else    SetState(PluginState::Idle, "Upload error: " + err);
        });
}

// ─── Library ─────────────────────────────────────────────────────────────────

void LinkVST::DeletePhrase(int id) {
    if (mPreviewId == id) StopPreview();
    mApi.DeleteLibraryItem(id);
    RefreshLibrary();
}

void LinkVST::RefreshLibrary() {
    auto items = mApi.GetLibrary();
    std::lock_guard<std::mutex> lock(mMutex);
    mLibraryPhrases = std::move(items);
    if (GetUI()) GetUI()->SetAllControlsDirty();
}

// ─── Preview ─────────────────────────────────────────────────────────────────

void LinkVST::TogglePreview(int library_id) {
    if (mPreviewId == library_id && mPreview.IsPlaying()) {
        StopPreview();
        return;
    }
    mPreviewId = library_id;
    SetState(PluginState::Idle, "Loading preview…");
    mPreview.FetchAndPlay(library_id, LINKVST_API_URL, LINKVST_API_KEY,
        [this](std::string err) {
            SetState(PluginState::Idle, "Preview error: " + err);
            mPreviewId = -1;
        });
    SetState(PluginState::Idle, "Playing — listen through your DAW output");
}

// ─── Drag-out ────────────────────────────────────────────────────────────────

void LinkVST::BeginDragOut(int idx, float x, float y) {
    if (idx < 0 || idx >= (int)mGeneratedPhrases.size()) return;
    const auto& p = mGeneratedPhrases[idx];
    std::string fname = p.key + "_" + p.mode + "_" + p.phrase_type + ".mid";
    void* view = GetUI() ? GetUI()->GetPlatformContext() : nullptr;
    DoDragOut(p.midi_bytes, fname, view, x, y);
}

void LinkVST::BeginLibraryDragOut(int idx, float x, float y) {
    if (idx < 0 || idx >= (int)mLibraryPhrases.size()) return;
    const auto& item = mLibraryPhrases[idx];
    auto bytes = mApi.GetMidi(item.id);
    if (bytes.empty()) return;
    std::string fname = item.key + "_" + item.mode + "_" + item.phrase_type + ".mid";
    void* view = GetUI() ? GetUI()->GetPlatformContext() : nullptr;
    DoDragOut(bytes, fname, view, x, y);
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

void LinkVST::SetState(PluginState state, const std::string& msg) {
    mState         = state;
    mStatusMessage = msg;
    if (GetUI()) GetUI()->SetAllControlsDirty();
}
