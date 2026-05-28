/**
 * GeneratePanel — upper 260px of the LinkVST window.
 *
 * Layout (600×260):
 *   Row 1 (0–38):    [▶ Generate]  [Count ▾]  [Type ▾]  [Key ▾]  [Mode ▾]  [Bars ▾]
 *   Row 2 (38–52):   Status bar
 *   Tiles (52–250):  2×2 grid of phrase tiles (each with ▶/⏹ play + drag)
 *   Row 3 (250–260): Upload MIDI button
 */

#include "LinkVST.h"
#include "IControl.h"
#include "IGraphics.h"

using namespace iplug;
using namespace igraphics;

static constexpr float PANEL_H = 260.f;
static constexpr float W       = 600.f;

// ─── PhraseTile ───────────────────────────────────────────────────────────────
class PhraseTile : public IControl {
public:
    PhraseTile(LinkVST* plug, int index, const IRECT& bounds)
        : IControl(bounds), mPlug(plug), mIndex(index) {}

    void Draw(IGraphics& g) override {
        const auto& phrases = mPlug->GetGeneratedPhrases();
        bool valid = mIndex < (int)phrases.size();

        IColor bg     = valid ? IColor(255, 22, 24, 40) : IColor(255, 16, 17, 30);
        IColor border = mMouseOver ? IColor(255, 120, 90, 220)
                      : (valid     ? IColor(255,  50, 52, 80)
                                   : IColor(255,  35, 37, 55));

        g.FillRoundRect(bg, mRECT, 6.f);
        g.DrawRoundRect(border, mRECT, 6.f, nullptr, 1.5f);

        if (!valid) {
            g.DrawText(IText(14, IColor(255, 50, 52, 75)), "—", mRECT);
            return;
        }

        const auto& p = phrases[mIndex];
        bool playing = mPlug->IsPreviewPlaying(p.id);

        // Type badge (top-left)
        IRECT badgeR = mRECT.GetFromTop(20.f).GetFromLeft(120.f).GetPadded(3.f, 3.f, -3.f, -6.f);
        g.FillRoundRect(IColor(255, 70, 45, 160), badgeR, 3.f);
        g.DrawText(IText(9, IColor(255, 210, 190, 255)), p.phrase_type.c_str(), badgeR);

        // Key + mode
        IRECT keyR(mRECT.L + 4.f, mRECT.T + 22.f, mRECT.R - 4.f, mRECT.T + 44.f);
        std::string keyStr = p.key + " " + p.mode;
        g.DrawText(IText(14, IColor(255, 210, 200, 255), nullptr, EAlign::Near), keyStr.c_str(), keyR);

        // Bars · bpm
        IRECT metaR(mRECT.L + 4.f, mRECT.T + 44.f, mRECT.R - 4.f, mRECT.T + 58.f);
        std::string meta = std::to_string(p.bars) + " bars · " + std::to_string(p.tempo_bpm) + " bpm";
        g.DrawText(IText(10, IColor(255, 100, 100, 130)), meta.c_str(), metaR);

        // Description
        IRECT descR(mRECT.L + 4.f, mRECT.T + 60.f, mRECT.R - 4.f, mRECT.B - 26.f);
        std::string desc = p.description.size() > 55 ? p.description.substr(0, 52) + "…" : p.description;
        g.DrawText(IText(9, IColor(255, 80, 80, 110), nullptr, EAlign::Near), desc.c_str(), descR);

        // Play button
        IRECT playR = mRECT.GetFromBottom(24.f).GetFromLeft(60.f).GetPadded(2.f);
        IColor playBg = playing ? IColor(255, 22, 180, 80) : IColor(255, 50, 40, 100);
        g.FillRoundRect(playBg, playR, 4.f);
        g.DrawText(IText(10, IColor(255, 240, 240, 255)), playing ? "⏹ Stop" : "▶ Play", playR);

        // Drag hint
        if (mMouseOver) {
            IRECT dragR = mRECT.GetFromBottom(24.f).GetReducedFromLeft(64.f);
            g.DrawText(IText(9, IColor(255, 130, 110, 200)), "⟵ drag", dragR);
        }
    }

    void OnMouseDown(float x, float y, const IMouseMod& mod) override {
        const auto& phrases = mPlug->GetGeneratedPhrases();
        if (mIndex >= (int)phrases.size()) return;

        // Play button zone = bottom-left 60px
        IRECT playR = mRECT.GetFromBottom(24.f).GetFromLeft(60.f);
        if (playR.Contains(x, y)) {
            mPlug->TogglePreview(phrases[mIndex].id);
            SetDirty(false);
            return;
        }
        // Elsewhere = drag
        mPlug->BeginDragOut(mIndex, x, y);
    }

    void OnMouseOver(float x, float y, const IMouseMod& mod) override { mMouseOver = true;  SetDirty(false); }
    void OnMouseOut() override                                          { mMouseOver = false; SetDirty(false); }

private:
    LinkVST* mPlug;
    int      mIndex;
    bool     mMouseOver = false;
};

// ─── BuildGenerateUI ──────────────────────────────────────────────────────────
void BuildGenerateUI(IGraphics* g, LinkVST* plug) {
    // Top bar background
    g->AttachControl(new IPanelControl(IRECT(0, 0, W, PANEL_H), IColor(255, 13, 14, 22)));

    // Generate button
    g->AttachControl(new ILambdaControl(IRECT(8, 6, 120, 32),
        [](IControl* c, IGraphics& g) {
            IColor bg = c->IsMouseOver() ? IColor(255, 110, 80, 210) : IColor(255, 80, 55, 175);
            g.FillRoundRect(bg, c->GetRECT(), 5.f);
            g.DrawText(IText(12, IColor(255, 240, 240, 255), "Roboto-Bold"), "▶  Generate", c->GetRECT());
        },
        kNoValIdx,
        [plug](IControl*, const IMouseMod&) { plug->RequestGenerate(4); }
    ));

    // Status bar
    g->AttachControl(new ILambdaControl(IRECT(0, 34, W, 50),
        [plug](IControl*, IGraphics& g) {
            g.DrawText(IText(10, IColor(255, 100, 100, 130), nullptr, EAlign::Near),
                       plug->GetStatusMessage().c_str(),
                       IRECT(8, 34, W - 8, 50));
        }
    ));

    // 2×2 phrase tiles
    float tw = (W - 30.f) / 2.f, th = (PANEL_H - 100.f) / 2.f;
    for (int i = 0; i < 4; ++i) {
        float col = i % 2, row = i / 2;
        IRECT tile(10.f + col * (tw + 10.f),
                   52.f + row * (th + 10.f),
                   10.f + col * (tw + 10.f) + tw,
                   52.f + row * (th + 10.f) + th);
        g->AttachControl(new PhraseTile(plug, i, tile));
    }

    // Upload MIDI button
    g->AttachControl(new ILambdaControl(IRECT(W - 130, PANEL_H - 26, W - 6, PANEL_H - 6),
        [](IControl* c, IGraphics& g) {
            g.FillRoundRect(IColor(255, 28, 30, 50), c->GetRECT(), 4.f);
            g.DrawRoundRect(IColor(255, 60, 62, 90), c->GetRECT(), 4.f);
            g.DrawText(IText(10, IColor(255, 140, 140, 170)), "↑ Upload MIDI", c->GetRECT());
        },
        kNoValIdx,
        [plug](IControl* c, const IMouseMod&) {
            WDL_String path;
            if (c->GetUI()->PromptForFile(path, EFileAction::Open, "mid midi"))
                plug->UploadMidiFile(path.Get());
        }
    ));
}
