/**
 * LibraryPanel — saved phrase browser for LinkVST.
 *
 * Layout (600×440, rendered below the main generate panel when visible):
 *
 *   ┌──────────────────────────────────────────────────────────────┐
 *   │ LIBRARY  [search____]  [Type ▾] [Key ▾]  [Refresh]  [✕ Close]│
 *   ├──────────────────────────────────────────────────────────────┤
 *   │ ▶  C major  chord_prog  4b·120  A rich warm progression…  [↓]│
 *   │ ▶  F# minor  melody     8b·95   Ascending modal line…     [↓]│
 *   │    … (scrollable)                                            │
 *   └──────────────────────────────────────────────────────────────┘
 *
 * Each row: play button | key+mode | type badge | bars·bpm | description | drag-handle
 */

#include "LinkVST.h"
#include "IControl.h"
#include "IGraphics.h"
#include <algorithm>
#include <string>

using namespace iplug;
using namespace igraphics;

static constexpr float ROW_H   = 36.f;
static constexpr float HEADER_H = 42.f;
static constexpr int   MAX_VISIBLE = 9;

// ─── Type badge color ─────────────────────────────────────────────────────────
static IColor TypeColor(const std::string& t) {
    if (t == "chord_progression") return IColor(255, 90,  60, 180);
    if (t == "melody")            return IColor(255, 60, 140, 180);
    if (t == "arpeggio")          return IColor(255, 60, 180, 130);
    return                               IColor(255, 180, 130,  60);  // bassline
}

// ─── LibraryRow ───────────────────────────────────────────────────────────────
class LibraryRow : public IControl {
public:
    LibraryRow(LinkVST* plug, int data_index, const IRECT& bounds)
        : IControl(bounds), mPlug(plug), mIndex(data_index) {}

    void Draw(IGraphics& g) override {
        const auto& items = mPlug->GetLibraryPhrases();
        if (mIndex >= (int)items.size()) return;
        const auto& item = items[mIndex];

        IColor bg = mMouseOver ? IColor(255, 28, 30, 50) : IColor(255, 18, 20, 36);
        g.FillRect(bg, mRECT);
        g.DrawLine(IColor(255, 40, 42, 65), mRECT.L, mRECT.B, mRECT.R, mRECT.B);

        // Play button
        IRECT playR = mRECT.GetFromLeft(36.f).GetPadded(-8.f);
        bool isPlaying = mPlug->IsPreviewPlaying(item.id);
        IColor playC = isPlaying ? IColor(255, 34, 197, 94) : IColor(255, 100, 100, 140);
        g.DrawText(IText(14, playC), isPlaying ? "⏹" : "▶", playR);

        // Key + mode
        IRECT keyR(mRECT.L + 36.f, mRECT.T, mRECT.L + 130.f, mRECT.B);
        std::string keyLabel = item.key + " " + item.mode;
        g.DrawText(IText(12, IColor(255, 200, 200, 220), nullptr, EAlign::Near), keyLabel.c_str(), keyR.GetPadded(-4.f));

        // Type badge
        IRECT badgeR(mRECT.L + 130.f, mRECT.T + 8.f, mRECT.L + 220.f, mRECT.B - 8.f);
        g.FillRoundRect(TypeColor(item.phrase_type), badgeR, 3.f);
        std::string typeShort = item.phrase_type.substr(0, 10);
        g.DrawText(IText(9, IColor(255, 240, 240, 255), "Roboto-Bold"), typeShort.c_str(), badgeR);

        // Bars · bpm
        IRECT metaR(mRECT.L + 225.f, mRECT.T, mRECT.L + 310.f, mRECT.B);
        std::string meta = std::to_string(item.bars) + "b·" + std::to_string(item.tempo_bpm);
        g.DrawText(IText(10, IColor(255, 100, 100, 130)), meta.c_str(), metaR);

        // Description (truncated)
        IRECT descR(mRECT.L + 315.f, mRECT.T, mRECT.R - 70.f, mRECT.B);
        std::string desc = item.description.size() > 45
                         ? item.description.substr(0, 42) + "…"
                         : item.description;
        g.DrawText(IText(10, IColor(255, 80, 80, 110), nullptr, EAlign::Near), desc.c_str(), descR.GetPadded(-4.f));

        // Drag hint on hover
        if (mMouseOver) {
            IRECT dragR = mRECT.GetFromRight(66.f).GetPadded(-4.f);
            g.DrawText(IText(10, IColor(255, 130, 110, 200)), "drag ⟵", dragR);
        }
    }

    void OnMouseDown(float x, float y, const IMouseMod& mod) override {
        const auto& items = mPlug->GetLibraryPhrases();
        if (mIndex >= (int)items.size()) return;
        const auto& item = items[mIndex];

        // Play button zone
        if (x < mRECT.L + 36.f) {
            mPlug->TogglePreview(item.id);
            SetDirty(false);
            return;
        }
        // Rest of row = drag-out
        mPlug->BeginLibraryDragOut(mIndex, x, y);
    }

    void OnMouseOver(float x, float y, const IMouseMod& mod) override { mMouseOver = true;  SetDirty(false); }
    void OnMouseOut() override                                          { mMouseOver = false; SetDirty(false); }

private:
    LinkVST* mPlug;
    int      mIndex;
    bool     mMouseOver = false;
};

// ─── LibraryPanel ─────────────────────────────────────────────────────────────
class LibraryPanelControl : public IControl {
public:
    LibraryPanelControl(LinkVST* plug, const IRECT& bounds)
        : IControl(bounds), mPlug(plug) {}

    void Draw(IGraphics& g) override {
        // Background
        g.FillRect(IColor(255, 12, 13, 24), mRECT);
        g.DrawRect(IColor(255, 50, 52, 80), mRECT, nullptr, 1.f);

        // Header label
        IRECT hdrR = mRECT.GetFromTop(HEADER_H);
        g.FillRect(IColor(255, 16, 17, 30), hdrR);
        g.DrawLine(IColor(255, 50, 52, 80), hdrR.L, hdrR.B, hdrR.R, hdrR.B);
        g.DrawText(IText(11, IColor(255, 120, 120, 160), nullptr, EAlign::Near),
                   "LIBRARY", IRECT(hdrR.L + 10.f, hdrR.T, hdrR.L + 80.f, hdrR.B));

        // Item count
        auto count = mPlug->GetLibraryPhrases().size();
        auto filtered = FilteredItems();
        std::string countStr = std::to_string(filtered.size()) + " / " + std::to_string(count);
        g.DrawText(IText(10, IColor(255, 80, 80, 110)),
                   countStr.c_str(),
                   IRECT(hdrR.L + 80.f, hdrR.T, hdrR.L + 160.f, hdrR.B));

        // Search box visual (actual input is via keyboard hook)
        IRECT searchR(hdrR.R - 200.f, hdrR.T + 8.f, hdrR.R - 10.f, hdrR.B - 8.f);
        g.FillRoundRect(IColor(255, 22, 24, 40), searchR, 4.f);
        g.DrawRoundRect(IColor(255, 60, 62, 90), searchR, 4.f);
        std::string searchDisplay = mSearch.empty() ? "search…" : mSearch;
        IColor searchColor = mSearch.empty() ? IColor(255, 60, 60, 80) : IColor(255, 180, 180, 200);
        g.DrawText(IText(11, searchColor, nullptr, EAlign::Near),
                   searchDisplay.c_str(), searchR.GetPadded(-6.f));

        // Empty state
        if (filtered.empty()) {
            IRECT emptyR = mRECT.GetReducedFromTop(HEADER_H);
            g.DrawText(IText(12, IColor(255, 60, 60, 80)), "No phrases found.", emptyR);
        }
    }

    void OnMouseDown(float x, float y, const IMouseMod& mod) override {
        // Click on search box = focus for keyboard input
        IRECT hdrR = mRECT.GetFromTop(HEADER_H);
        IRECT searchR(hdrR.R - 200.f, hdrR.T + 8.f, hdrR.R - 10.f, hdrR.B - 8.f);
        mSearchFocused = searchR.Contains(x, y);
        SetDirty(false);
    }

    void OnKeyDown(float x, float y, const IKeyPress& key) override {
        if (!mSearchFocused) return;
        if (key.VK == kVK_BACK && !mSearch.empty())
            mSearch.pop_back();
        else if (key.utf8[0] >= 32)
            mSearch += key.utf8[0];
        RebuildRows();
        SetDirty(false);
    }

    void RebuildRows() {
        auto* g = GetUI();
        if (!g) return;
        // Remove old row controls (tags 2000–2099)
        for (int i = 2000; i < 2100; ++i)
            if (auto* ctrl = g->GetControlWithTag(i)) {
                g->RemoveControl(ctrl);
            }

        auto indices = FilteredItems();
        int visible = std::min((int)indices.size(), MAX_VISIBLE);

        for (int i = 0; i < visible; ++i) {
            float rowY = mRECT.T + HEADER_H + i * ROW_H;
            IRECT rowR(mRECT.L, rowY, mRECT.R, rowY + ROW_H);
            auto* row = new LibraryRow(mPlug, indices[i], rowR);
            row->SetTag(2000 + i);
            g->AttachControl(row);
        }
        SetDirty(false);
    }

    std::vector<int> FilteredItems() {
        const auto& items = mPlug->GetLibraryPhrases();
        std::vector<int> out;
        std::string lSearch = mSearch;
        std::transform(lSearch.begin(), lSearch.end(), lSearch.begin(), ::tolower);

        for (int i = 0; i < (int)items.size(); ++i) {
            if (!lSearch.empty()) {
                std::string s = items[i].key + " " + items[i].mode + " " +
                                items[i].phrase_type + " " + items[i].description;
                std::transform(s.begin(), s.end(), s.begin(), ::tolower);
                if (s.find(lSearch) == std::string::npos) continue;
            }
            out.push_back(i);
        }
        return out;
    }

private:
    LinkVST*    mPlug;
    std::string mSearch;
    bool        mSearchFocused = false;
};

// ─── Builder function — called from LinkVST::OnUIOpen ─────────────────────────
void BuildLibraryPanel(IGraphics* g, LinkVST* plug, const IRECT& bounds) {
    auto* panel = new LibraryPanelControl(plug, bounds);
    panel->SetTag(1999);
    g->AttachControl(panel);
    panel->RebuildRows();
}
