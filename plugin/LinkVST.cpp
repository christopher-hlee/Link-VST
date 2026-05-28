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

void LinkVST::BeginDragOut(int phrase_index) {
  if (phrase_index < 0 || phrase_index >= (int)mGeneratedPhrases.size()) return;
  const auto& phrase = mGeneratedPhrases[phrase_index];
  std::string filename = phrase.key + "_" + phrase.mode + "_" +
                         phrase.phrase_type + ".mid";
  DoDragOut(phrase.midi_bytes, filename);
}

void LinkVST::SetState(PluginState state, const std::string& msg) {
  mState = state;
  mStatusMessage = msg;
  // Trigger UI repaint
  if (GetUI()) GetUI()->SetAllControlsDirty();
}

// ---------------------------------------------------------------------------
// Platform drag-out
// ---------------------------------------------------------------------------

#ifdef OS_WIN
#include <windows.h>
#include <shlobj.h>
#include <objidl.h>

void LinkVST::DoDragOut(const std::vector<uint8_t>& midi_bytes,
                         const std::string& filename) {
  // Write temp file, then initiate OLE drag-drop with CFSTR_FILECONTENTS
  std::string tmp = std::string(getenv("TEMP") ? getenv("TEMP") : "C:\\Temp")
                    + "\\" + filename;
  std::ofstream f(tmp, std::ios::binary);
  f.write(reinterpret_cast<const char*>(midi_bytes.data()), midi_bytes.size());
  f.close();

  // Build STGMEDIUM with HGLOBAL containing the temp path
  wchar_t wpath[MAX_PATH];
  MultiByteToWideChar(CP_UTF8, 0, tmp.c_str(), -1, wpath, MAX_PATH);

  DROPFILES* df;
  size_t path_len = wcslen(wpath) + 1;
  size_t total = sizeof(DROPFILES) + (path_len + 1) * sizeof(wchar_t);
  HGLOBAL hg = GlobalAlloc(GHND, total);
  df = (DROPFILES*)GlobalLock(hg);
  df->pFiles = sizeof(DROPFILES);
  df->fWide  = TRUE;
  memcpy((char*)df + sizeof(DROPFILES), wpath, path_len * sizeof(wchar_t));
  GlobalUnlock(hg);

  FORMATETC fmt = { CF_HDROP, nullptr, DVASPECT_CONTENT, -1, TYMED_HGLOBAL };
  STGMEDIUM stg = { TYMED_HGLOBAL, { hg }, nullptr };

  // IDataObject / IDropSource implementation omitted for brevity;
  // use iPlug2's built-in drag helper or a lightweight COM wrapper.
  // DoDragDrop(pDataObj, pDropSource, DROPEFFECT_COPY, &effect);
  GlobalFree(hg);
}

#elif defined(OS_MAC)
#include <AppKit/AppKit.h>

void LinkVST::DoDragOut(const std::vector<uint8_t>& midi_bytes,
                         const std::string& filename) {
  NSString* tmpDir = NSTemporaryDirectory();
  NSString* path   = [tmpDir stringByAppendingPathComponent:
                       [NSString stringWithUTF8String:filename.c_str()]];
  NSData* data = [NSData dataWithBytes:midi_bytes.data() length:midi_bytes.size()];
  [data writeToFile:path atomically:YES];

  NSURL* url = [NSURL fileURLWithPath:path];
  NSPasteboard* pb = [NSPasteboard pasteboardWithUniqueName];
  [pb declareTypes:@[NSPasteboardTypeFileURL] owner:nil];
  [pb writeObjects:@[url]];

  // The actual drag event must be initiated from the mouse-down handler
  // in the NSView subclass. Store the path and trigger from the UI layer.
  // See GeneratePanel.mm for the full NSView drag implementation.
}

#else
void LinkVST::DoDragOut(const std::vector<uint8_t>&, const std::string&) {}
#endif
