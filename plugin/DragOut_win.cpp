/**
 * DragOut_win.cpp — Windows OLE drag-out for LinkVST.
 *
 * Approach: CF_HDROP with a real temp file.
 *   1. Write MIDI bytes to %TEMP%\<uuid>_<filename>.
 *   2. Build an IDataObject that exposes CF_HDROP pointing to the file.
 *   3. Implement a minimal IDropSource (just answer "copy only, no feedback needed").
 *   4. Call OLE DoDragDrop() — blocks until the user drops or cancels.
 *   5. Delete the temp file after the drag completes.
 *
 * Using CFSTR_FILEDESCRIPTOR + CFSTR_FILECONTENTS (virtual file) is cleaner
 * but many DAWs only accept CF_HDROP. Real file is the most compatible path.
 */

#ifdef OS_WIN

#include "LinkVST.h"
#include <windows.h>
#include <shlobj.h>
#include <objidl.h>
#include <ole2.h>
#include <combaseapi.h>
#include <fstream>
#include <string>
#include <cassert>

// ---------------------------------------------------------------------------
// Minimal IDropSource
// ---------------------------------------------------------------------------

class MidiDropSource final : public IDropSource {
public:
    ULONG   mRef = 1;

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override {
        if (riid == IID_IUnknown || riid == IID_IDropSource) {
            *ppv = this;
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef()  override { return ++mRef; }
    ULONG STDMETHODCALLTYPE Release() override {
        if (--mRef == 0) { delete this; return 0; }
        return mRef;
    }

    // Allow copy only; no move.
    HRESULT STDMETHODCALLTYPE QueryContinueDrag(BOOL fEscapePressed,
                                                DWORD grfKeyState) override {
        if (fEscapePressed)              return DRAGDROP_S_CANCEL;
        if (!(grfKeyState & MK_LBUTTON)) return DRAGDROP_S_DROP;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GiveFeedback(DWORD dwEffect) override {
        return DRAGDROP_S_USEDEFAULTCURSORS;
    }
};

// ---------------------------------------------------------------------------
// Minimal IDataObject — exposes CF_HDROP only
// ---------------------------------------------------------------------------

class MidiDataObject final : public IDataObject {
public:
    ULONG  mRef  = 1;
    HGLOBAL mHDrop = nullptr;

    explicit MidiDataObject(HGLOBAL hDrop) : mHDrop(hDrop) {}
    ~MidiDataObject() { if (mHDrop) GlobalFree(mHDrop); }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override {
        if (riid == IID_IUnknown || riid == IID_IDataObject) {
            *ppv = this;
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef()  override { return ++mRef; }
    ULONG STDMETHODCALLTYPE Release() override {
        if (--mRef == 0) { delete this; return 0; }
        return mRef;
    }

    HRESULT STDMETHODCALLTYPE GetData(FORMATETC* fmt, STGMEDIUM* stg) override {
        if (!fmt || !stg) return E_INVALIDARG;
        if (fmt->cfFormat != CF_HDROP || !(fmt->tymed & TYMED_HGLOBAL))
            return DV_E_FORMATETC;

        // Duplicate the HGLOBAL so the caller can free it independently
        SIZE_T sz = GlobalSize(mHDrop);
        HGLOBAL copy = GlobalAlloc(GMEM_MOVEABLE, sz);
        if (!copy) return E_OUTOFMEMORY;
        void* dst = GlobalLock(copy);
        void* src = GlobalLock(mHDrop);
        memcpy(dst, src, sz);
        GlobalUnlock(mHDrop);
        GlobalUnlock(copy);

        stg->tymed          = TYMED_HGLOBAL;
        stg->hGlobal        = copy;
        stg->pUnkForRelease = nullptr;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetDataHere(FORMATETC*, STGMEDIUM*)      override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE QueryGetData(FORMATETC* fmt)             override {
        if (fmt && fmt->cfFormat == CF_HDROP && (fmt->tymed & TYMED_HGLOBAL))
            return S_OK;
        return DV_E_FORMATETC;
    }
    HRESULT STDMETHODCALLTYPE GetCanonicalFormatEtc(FORMATETC*, FORMATETC* out) override {
        if (out) out->ptd = nullptr;
        return E_NOTIMPL;
    }
    HRESULT STDMETHODCALLTYPE SetData(FORMATETC*, STGMEDIUM*, BOOL)    override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE DAdvise(FORMATETC*, DWORD, IAdviseSink*, DWORD*) override { return OLE_E_ADVISENOTSUPPORTED; }
    HRESULT STDMETHODCALLTYPE DUnadvise(DWORD)                         override { return OLE_E_ADVISENOTSUPPORTED; }
    HRESULT STDMETHODCALLTYPE EnumDAdvise(IEnumSTATDATA**)             override { return OLE_E_ADVISENOTSUPPORTED; }

    HRESULT STDMETHODCALLTYPE EnumFormatEtc(DWORD dwDirection,
                                             IEnumFORMATETC** ppEnum) override {
        if (dwDirection != DATADIR_GET) return E_NOTIMPL;
        FORMATETC fmts[] = {
            { CF_HDROP, nullptr, DVASPECT_CONTENT, -1, TYMED_HGLOBAL }
        };
        return SHCreateStdEnumFmtEtc(1, fmts, ppEnum);
    }
};

// ---------------------------------------------------------------------------
// Build CF_HDROP HGLOBAL for a single wide path
// ---------------------------------------------------------------------------

static HGLOBAL BuildHDrop(const std::wstring& path) {
    // DROPFILES header + null-terminated path + extra null terminator
    size_t pathLen = path.size() + 1;  // includes null
    size_t total   = sizeof(DROPFILES) + (pathLen + 1) * sizeof(wchar_t);

    HGLOBAL hg = GlobalAlloc(GHND, total);
    if (!hg) return nullptr;

    DROPFILES* df = reinterpret_cast<DROPFILES*>(GlobalLock(hg));
    df->pFiles = sizeof(DROPFILES);
    df->fWide  = TRUE;
    df->pt     = { 0, 0 };
    df->fNC    = FALSE;

    wchar_t* dest = reinterpret_cast<wchar_t*>(
        reinterpret_cast<char*>(df) + sizeof(DROPFILES));
    memcpy(dest, path.c_str(), pathLen * sizeof(wchar_t));
    dest[pathLen] = L'\0';  // double-null terminator

    GlobalUnlock(hg);
    return hg;
}

// ---------------------------------------------------------------------------
// UUID helper (no external deps)
// ---------------------------------------------------------------------------

static std::wstring NewUUID() {
    UUID uuid;
    UuidCreate(&uuid);
    wchar_t* str = nullptr;
    UuidToStringW(&uuid, reinterpret_cast<RPC_WSTR*>(&str));
    std::wstring result(str);
    RpcStringFreeW(reinterpret_cast<RPC_WSTR*>(&str));
    return result;
}

// ---------------------------------------------------------------------------
// LinkVST::DoDragOut (Windows)
// ---------------------------------------------------------------------------

void LinkVST::DoDragOut(const std::vector<uint8_t>& midi_bytes,
                         const std::string& filename,
                         void* /*platform_view*/,
                         float /*x*/, float /*y*/)
{
    // 1. Build temp path
    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);

    // Convert filename to wide
    int wlen = MultiByteToWideChar(CP_UTF8, 0, filename.c_str(), -1, nullptr, 0);
    std::wstring wFilename(wlen, 0);
    MultiByteToWideChar(CP_UTF8, 0, filename.c_str(), -1, wFilename.data(), wlen);
    wFilename.resize(wlen - 1);  // strip null

    std::wstring uuid   = NewUUID();
    std::wstring tmpPath = std::wstring(tempDir) + L"linkvst_" + uuid + L"_" + wFilename;

    // 2. Write file
    HANDLE hFile = CreateFileW(tmpPath.c_str(), GENERIC_WRITE, 0, nullptr,
                               CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hFile == INVALID_HANDLE_VALUE) return;

    DWORD written = 0;
    WriteFile(hFile, midi_bytes.data(), (DWORD)midi_bytes.size(), &written, nullptr);
    CloseHandle(hFile);

    // 3. Build IDataObject
    HGLOBAL hDrop = BuildHDrop(tmpPath);
    if (!hDrop) { DeleteFileW(tmpPath.c_str()); return; }

    MidiDataObject* dataObj = new MidiDataObject(hDrop);
    MidiDropSource* dropSrc = new MidiDropSource();

    // 4. Run OLE drag-drop (blocks until drop or cancel)
    OleInitialize(nullptr);
    DWORD effect = 0;
    DoDragDrop(dataObj, dropSrc, DROPEFFECT_COPY, &effect);

    dataObj->Release();
    dropSrc->Release();

    // 5. Delete temp file — DAW has already copied it
    DeleteFileW(tmpPath.c_str());
}

#endif // OS_WIN
