#include "LinkVST.h"
#include "IPlug_include_in_plug_src.h"
#include <fstream>
#include <filesystem>

// Build-time config — override per-target in CMakeLists
#ifndef LINKVST_API_URL
#define LINKVST_API_URL "http://localhost:8002"
#endif
#ifndef LINKVST_API_KEY
#define LINKVST_API_KEY "linkvst-dev"
#endif

// Forward declarations from panel builders
void BuildGenerateUI(iplug::igraphics::IGraphics* g, LinkVST* plug);
void BuildLibraryPanel(iplug::igraphics::IGraphics* g, LinkVST* plug,
                        const iplug::igraphics::IRECT& bounds);

// ─── Constructor ─────────────────────────────────────────────────────────────

LinkVST::LinkVST(const iplug::InstanceInfo& info)
    : iplug::Plugin(info, iplug::MakeConfig(0, 1))
    , mApi(LINKVST_API_URL, LINKVST_API_KEY)
{
    // Declare stereo output (0 in, 2 out) for preview playback
    SetChannelIO({iplug::IOConfig(0, 2)});
    RefreshLibrary();
}

// ─── UI ──────────────────────────────────────────────────────────────────────

void LinkVST::OnUIOpen() {
    auto* g = GetUI();
    if (!g) return;

    constexpr int W = 600, H = 440;
    constexpr float SPLIT = 260.f;  // generate panel height

    BuildGenerateUI(g, this);

    // Library panel fills the lower portion
    iplug::igraphics::IRECT libBounds(0.f, SPLIT, (float)W, (float)H);
    BuildLibraryPanel(g, this, libBounds);

    RefreshLibrary();
}

void LinkVST::OnUIClose() {
    StopPreview();
}

// ─── Audio output ─────────────────────────────────────────────────────────────

void LinkVST::ProcessBlock(iplug::sample** inputs, iplug::sample** outputs, int nFrames) {
    mPreview.Process(outputs, nFrames, GetSampleRate());
}

// ─── Generate ────────────────────────────────────────────────────────────────

void LinkVST::RequestGenerate(int count, const std::string& phrase_type,
                               const std::string& key, const std::string& mode,
                               int bars, const std::string& hint) {
    SetState(PluginState::Generating, "Generating with Claude…");

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

// ─── Upload ──────────────────────────────────────────────────────────────────

void LinkVST::UploadMidiFile(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return;
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(f)), {});
    std::string filename = std::filesystem::path(path).filename().string();

    SetState(PluginState::Uploading, "Uploading " + filename + "…");

    mApi.UploadMidi(filename, bytes,
        [this, filename](bool ok, std::string msg, std::string err) {
            if (ok) SetState(PluginState::Idle, "Uploaded: " + filename + " — taste profile updated.");
            else    SetState(PluginState::Idle, "Upload error: " + err);
        });
}

// ─── Library ─────────────────────────────────────────────────────────────────

void LinkVST::SavePhrase(int /*index*/) {
    RefreshLibrary();
}

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
        SetState(PluginState::Idle, "");
        return;
    }
    mPreviewId = library_id;
    SetState(PluginState::Idle, "Loading preview…");

    mPreview.FetchAndPlay(library_id, LINKVST_API_URL, LINKVST_API_KEY,
        [this](std::string err) {
            SetState(PluginState::Idle, "Preview error: " + err);
            mPreviewId = -1;
        });

    SetState(PluginState::Idle, "Playing — listen in your DAW's output");
}

// ─── Drag-out ────────────────────────────────────────────────────────────────

void LinkVST::BeginDragOut(int phrase_index, float x, float y) {
    if (phrase_index < 0 || phrase_index >= (int)mGeneratedPhrases.size()) return;
    const auto& phrase = mGeneratedPhrases[phrase_index];
    std::string filename = phrase.key + "_" + phrase.mode + "_" + phrase.phrase_type + ".mid";
    void* view = GetUI() ? GetUI()->GetPlatformContext() : nullptr;
    DoDragOut(phrase.midi_bytes, filename, view, x, y);
}

void LinkVST::BeginLibraryDragOut(int library_index, float x, float y) {
    if (library_index < 0 || library_index >= (int)mLibraryPhrases.size()) return;
    const auto& item = mLibraryPhrases[library_index];

    // Fetch MIDI bytes on demand (library items don't carry them in memory)
    auto bytes = mApi.GetMidi(item.id);
    if (bytes.empty()) return;

    std::string filename = item.key + "_" + item.mode + "_" + item.phrase_type + ".mid";
    void* view = GetUI() ? GetUI()->GetPlatformContext() : nullptr;
    DoDragOut(bytes, filename, view, x, y);
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

void LinkVST::SetState(PluginState state, const std::string& msg) {
    mState         = state;
    mStatusMessage = msg;
    if (GetUI()) GetUI()->SetAllControlsDirty();
}
