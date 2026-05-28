/**
 * GeneratePanel — upper 260px of the LinkVST window.
 *
 * Layout (600 × 260):
 *   Row 1  (4–32):   [▶ Gen]  [Preset ▾]  [Type ▾]  [Key ▾]  [Mode ▾]  [Bars ▾]  [Count ▾]
 *   Row 2  (36–56):  Hint: [______________]   Swing ←[===]→   VelVar ←[===]→
 *   Status (58–72):  ← message
 *   Tiles  (74–248): 2×2 phrase tiles (each 86px tall, 285px wide)
 *   Upload (250–260): Upload MIDI button
 */

#include "LinkVST.h"
#include "IControl.h"
#include "IGraphics.h"
#include <vector>
#include <string>
#include <functional>

using namespace iplug;
using namespace igraphics;

static constexpr float W       = 600.f;
static constexpr float PANEL_H = 260.f;

// ─── Inline dropdown control ───────────────────────────────────────────────────
class DropdownCtrl : public IControl {
public:
    DropdownCtrl(const IRECT& bounds,
                 const char* label,
                 std::vector<std::string> options,
                 std::function<void(const std::string&)> onChange)
        : IControl(bounds), mLabel(label), mOptions(std::move(options))
        , mOnChange(std::move(onChange)), mValue(mOptions.empty() ? "" : mOptions[0]) {}

    void Draw(IGraphics& g) override {
        bool hover = IsMouseOver();
        g.FillRoundRect(hover ? IColor(255,32,34,55) : IColor(255,22,24,40), mRECT, 4.f);
        g.DrawRoundRect(hover ? IColor(255,90,70,190) : IColor(255,45,47,72), mRECT, 4.f);

        // Label above
        if (mLabel[0]) {
            IRECT lblR(mRECT.L, mRECT.T - 14.f, mRECT.R, mRECT.T - 2.f);
            g.DrawText(IText(8, IColor(255,80,80,110)), mLabel, lblR);
        }
        // Value text + chevron
        IRECT valR = mRECT.GetPadded(-4.f).GetReducedFromRight(12.f);
        g.DrawText(IText(10, IColor(255,185,185,210), nullptr, EAlign::Near),
                   mValue.empty() ? "Any" : mValue.c_str(), valR);
        g.DrawText(IText(8, IColor(255,100,100,140)), "▾", mRECT.GetFromRight(14.f));
    }

    void OnMouseDown(float, float, const IMouseMod&) override {
        if (!GetUI()) return;
        IPopupMenu menu;
        for (const auto& opt : mOptions)
            menu.AddItem(opt.c_str());
        GetUI()->CreatePopupMenu(*this, menu, mRECT);
    }

    void OnPopupMenuSelection(IPopupMenu* menu, int) override {
        if (!menu) return;
        int idx = menu->GetChosenItemIdx();
        if (idx >= 0 && idx < (int)mOptions.size()) {
            mValue = mOptions[idx];
            if (mOnChange) mOnChange(mValue);
            SetDirty(false);
        }
    }

    void SetValue(const std::string& v) { mValue = v; SetDirty(false); }
    const std::string& Get() const { return mValue; }

private:
    std::string mLabel;
    std::vector<std::string> mOptions;
    std::function<void(const std::string&)> mOnChange;
    std::string mValue;
};

// ─── Compact horizontal slider ─────────────────────────────────────────────────
class SliderCtrl : public IControl {
public:
    SliderCtrl(const IRECT& bounds, const char* label,
               float lo, float hi, float init,
               std::function<void(float)> onChange)
        : IControl(bounds), mLabel(label), mLo(lo), mHi(hi), mVal(init)
        , mOnChange(std::move(onChange)) {}

    void Draw(IGraphics& g) override {
        // Label
        IRECT lblR = mRECT.GetFromLeft(56.f);
        g.DrawText(IText(9, IColor(255,80,80,110), nullptr, EAlign::Near), mLabel, lblR.GetPadded(-2.f));
        // Track
        IRECT trackR(mRECT.L + 60.f, mRECT.MH() - 2.f, mRECT.R - 30.f, mRECT.MH() + 2.f);
        g.FillRoundRect(IColor(255,30,32,55), trackR, 2.f);
        float pct = (mVal - mLo) / (mHi - mLo);
        IRECT fillR(trackR.L, trackR.T, trackR.L + pct * trackR.W(), trackR.B);
        g.FillRoundRect(IColor(255,100,70,200), fillR, 2.f);
        // Thumb
        float tx = trackR.L + pct * trackR.W();
        g.FillCircle(IColor(255,160,130,240), tx, mRECT.MH(), 5.f);
        // Value text
        IRECT valR = mRECT.GetFromRight(28.f);
        char buf[16]; snprintf(buf, sizeof(buf), "%.0f%%", pct * 100.f);
        g.DrawText(IText(9, IColor(255,130,110,210), nullptr, EAlign::Far), buf, valR);
    }

    void OnMouseDown(float x, float, const IMouseMod&) override { SetFromX(x); }
    void OnMouseDrag(float x, float, float, float, const IMouseMod&) override { SetFromX(x); }

    float GetValue() const { return mVal; }
    void SetValue(float v) { mVal = std::max(mLo, std::min(mHi, v)); SetDirty(false); }

private:
    std::string mLabel;
    float mLo, mHi, mVal;
    std::function<void(float)> mOnChange;

    void SetFromX(float x) {
        float trackL = mRECT.L + 60.f, trackR2 = mRECT.R - 30.f;
        float pct = (x - trackL) / (trackR2 - trackL);
        mVal = mLo + std::max(0.f, std::min(1.f, pct)) * (mHi - mLo);
        if (mOnChange) mOnChange(mVal);
        SetDirty(false);
    }
};

// ─── PhraseTile ───────────────────────────────────────────────────────────────
class PhraseTile : public IControl {
public:
    PhraseTile(LinkVST* plug, int index, const IRECT& bounds)
        : IControl(bounds), mPlug(plug), mIndex(index) {}

    void Draw(IGraphics& g) override {
        const auto& phrases = mPlug->GetGeneratedPhrases();
        bool valid   = mIndex < (int)phrases.size();
        bool playing = valid && mPlug->IsPreviewPlaying(phrases[mIndex].id);

        IColor bg     = valid ? IColor(255,22,24,40)   : IColor(255,16,17,28);
        IColor border = playing   ? IColor(255,34,197,94)
                      : mMouseOver ? IColor(255,100,76,210)
                      : (valid     ? IColor(255,44,46,70)
                                   : IColor(255,28,30,48));

        g.FillRoundRect(bg, mRECT, 5.f);
        g.DrawRoundRect(border, mRECT, 5.f, nullptr, playing ? 2.f : 1.f);

        if (!valid) {
            g.DrawText(IText(13, IColor(255,40,42,65)), "—", mRECT);
            return;
        }

        const auto& p = phrases[mIndex];

        // Type badge
        IRECT badgeR(mRECT.L + 4.f, mRECT.T + 4.f, mRECT.L + 100.f, mRECT.T + 16.f);
        g.FillRoundRect(IColor(255,60,38,140), badgeR, 3.f);
        g.DrawText(IText(8, IColor(255,190,170,255)), p.phrase_type.c_str(), badgeR);

        // Key + mode
        IRECT keyR(mRECT.L + 4.f, mRECT.T + 18.f, mRECT.R - 4.f, mRECT.T + 36.f);
        std::string keyStr = p.key + " " + p.mode;
        g.DrawText(IText(13, IColor(255,200,195,255), nullptr, EAlign::Near), keyStr.c_str(), keyR);

        // Bars · bpm
        IRECT metaR(mRECT.L + 4.f, mRECT.T + 37.f, mRECT.R - 4.f, mRECT.T + 50.f);
        std::string meta = std::to_string(p.bars) + "b · " + std::to_string(p.tempo_bpm) + "bpm";
        g.DrawText(IText(9, IColor(255,80,80,110)), meta.c_str(), metaR);

        // Description
        IRECT descR(mRECT.L + 4.f, mRECT.T + 52.f, mRECT.R - 4.f, mRECT.B - 22.f);
        std::string desc = p.description.size() > 50 ? p.description.substr(0,47) + "…" : p.description;
        g.DrawText(IText(8, IColor(255,72,72,96), nullptr, EAlign::Near), desc.c_str(), descR);

        // Play button
        IRECT playR(mRECT.L + 4.f, mRECT.B - 20.f, mRECT.L + 60.f, mRECT.B - 4.f);
        g.FillRoundRect(playing ? IColor(255,18,160,70) : IColor(255,50,38,110), playR, 3.f);
        g.DrawText(IText(9, IColor(255,240,240,255)), playing ? "⏹ Stop" : "▶ Play", playR);

        if (mMouseOver)
            g.DrawText(IText(8, IColor(255,110,95,185)), "⟵ drag",
                       IRECT(mRECT.R - 50.f, mRECT.B - 20.f, mRECT.R - 2.f, mRECT.B - 4.f));
    }

    void OnMouseDown(float x, float y, const IMouseMod&) override {
        const auto& phrases = mPlug->GetGeneratedPhrases();
        if (mIndex >= (int)phrases.size()) return;
        IRECT playR(mRECT.L + 4.f, mRECT.B - 20.f, mRECT.L + 60.f, mRECT.B - 4.f);
        if (playR.Contains(x, y)) {
            mPlug->TogglePreview(phrases[mIndex].id);
            SetDirty(false);
        } else {
            mPlug->BeginDragOut(mIndex, x, y);
        }
    }

    void OnMouseOver(float, float, const IMouseMod&) override { mMouseOver = true;  SetDirty(false); }
    void OnMouseOut() override                                  { mMouseOver = false; SetDirty(false); }

private:
    LinkVST* mPlug;
    int      mIndex;
    bool     mMouseOver = false;
};

// ─── BuildGenerateUI ──────────────────────────────────────────────────────────
void BuildGenerateUI(IGraphics* g, LinkVST* plug) {
    // Background
    g->AttachControl(new IPanelControl(IRECT(0,0,W,PANEL_H), IColor(255,12,13,22)));

    float dropH = 24.f;
    float rowY  = 18.f;  // vertical center of the first row

    // Generate button
    g->AttachControl(new ILambdaControl(IRECT(6, rowY - 12.f, 96, rowY + 12.f),
        [](IControl* c, IGraphics& g) {
            IColor bg = c->IsMouseOver() ? IColor(255,100,76,210) : IColor(255,72,52,180);
            g.FillRoundRect(bg, c->GetRECT(), 4.f);
            g.DrawText(IText(11, IColor(255,240,240,255), "Roboto-Bold"), "▶  Generate", c->GetRECT());
        },
        kNoValIdx,
        [plug](IControl*, const IMouseMod&) { plug->RequestGenerate(); }
    ));

    // Type dropdown
    auto* typeCtrl = new DropdownCtrl(
        IRECT(100, rowY - 12.f, 180, rowY + 12.f), "Type",
        {"", "chord_progression", "melody", "arpeggio", "bassline"},
        [plug](const std::string& v) { plug->SetSetting("phrase_type", v); }
    );
    g->AttachControl(typeCtrl);

    // Key dropdown
    auto* keyCtrl = new DropdownCtrl(
        IRECT(184, rowY - 12.f, 248, rowY + 12.f), "Key",
        {"", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"},
        [plug](const std::string& v) { plug->SetSetting("key", v); }
    );
    g->AttachControl(keyCtrl);

    // Mode dropdown
    auto* modeCtrl = new DropdownCtrl(
        IRECT(252, rowY - 12.f, 348, rowY + 12.f), "Mode",
        {"", "major", "minor", "dorian", "phrygian", "lydian", "mixolydian", "locrian"},
        [plug](const std::string& v) { plug->SetSetting("mode", v); }
    );
    g->AttachControl(modeCtrl);

    // Bars dropdown
    auto* barsCtrl = new DropdownCtrl(
        IRECT(352, rowY - 12.f, 408, rowY + 12.f), "Bars",
        {"2", "4", "8", "16"},
        [plug](const std::string& v) { plug->SetSetting("bars", v); }
    );
    barsCtrl->SetValue("4");
    g->AttachControl(barsCtrl);

    // Count dropdown
    auto* countCtrl = new DropdownCtrl(
        IRECT(412, rowY - 12.f, 464, rowY + 12.f), "Count",
        {"1", "2", "4", "8"},
        [plug](const std::string& v) { plug->SetSetting("count", v); }
    );
    countCtrl->SetValue("4");
    g->AttachControl(countCtrl);

    // Row 2: Humanization sliders
    float row2Y = 48.f;

    g->AttachControl(new SliderCtrl(
        IRECT(6, row2Y, 200, row2Y + 18.f), "Swing",
        0.f, 1.f, 0.f,
        [plug](float v) { plug->SetHumanize("swing", v); }
    ));

    g->AttachControl(new SliderCtrl(
        IRECT(208, row2Y, 390, row2Y + 18.f), "Vel Var",
        0.f, 30.f, 0.f,
        [plug](float v) { plug->SetHumanize("vel_var", v); }
    ));

    // Status bar
    g->AttachControl(new ILambdaControl(IRECT(0, 62.f, W, 76.f),
        [plug](IControl*, IGraphics& g) {
            g.DrawText(IText(9, IColor(255,90,90,120), nullptr, EAlign::Near),
                       plug->GetStatusMessage().c_str(), IRECT(6.f, 62.f, W - 6.f, 76.f));
        }
    ));

    // Tiles (2×2)
    float tw = (W - 30.f) / 2.f, th = (PANEL_H - 110.f) / 2.f;
    for (int i = 0; i < 4; ++i) {
        float col = i % 2, row = i / 2;
        IRECT tile(10.f + col * (tw + 10.f),
                   78.f + row * (th + 8.f),
                   10.f + col * (tw + 10.f) + tw,
                   78.f + row * (th + 8.f) + th);
        g->AttachControl(new PhraseTile(plug, i, tile));
    }

    // Upload MIDI button
    g->AttachControl(new ILambdaControl(IRECT(W - 130.f, PANEL_H - 22.f, W - 6.f, PANEL_H - 4.f),
        [](IControl* c, IGraphics& g) {
            g.FillRoundRect(IColor(255,24,26,44), c->GetRECT(), 4.f);
            g.DrawRoundRect(IColor(255,50,52,80), c->GetRECT(), 4.f);
            g.DrawText(IText(9, IColor(255,120,120,150)), "↑ Upload MIDI", c->GetRECT());
        },
        kNoValIdx,
        [plug](IControl* c, const IMouseMod&) {
            WDL_String path;
            if (c->GetUI()->PromptForFile(path, EFileAction::Open, "mid midi"))
                plug->UploadMidiFile(path.Get());
        }
    ));
}
