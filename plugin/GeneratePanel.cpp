/**
 * GeneratePanel — main UI panel for LinkVST.
 *
 * Layout (600×400):
 *   Top bar:  [Generate ▶]  [Count: 1 2 4 8]  [Type ▾]  [Key ▾]  [Bars ▾]
 *   Center:   4 phrase tiles, each showing type/key/description
 *   Bottom:   Status bar + [Upload MIDI] button
 *
 * Each tile is draggable — mouse-down begins DoDragOut.
 */

#include "LinkVST.h"
#include "IControl.h"
#include "IGraphics.h"

using namespace iplug;
using namespace igraphics;

// ---------------------------------------------------------------------------
// PhraseTile — one draggable result cell
// ---------------------------------------------------------------------------
class PhraseTile : public IControl {
public:
  PhraseTile(LinkVST* plug, int index, const IRECT& bounds)
    : IControl(bounds), mPlug(plug), mIndex(index) {}

  void Draw(IGraphics& g) override {
    const auto& phrases = mPlug->GetGeneratedPhrases();
    bool valid = mIndex < (int)phrases.size();

    IColor bg = valid ? IColor(255, 30, 34, 48) : IColor(255, 20, 22, 32);
    IColor border = mMouseOver ? IColor(255, 120, 100, 220) : IColor(255, 60, 60, 80);

    g.FillRect(bg, mRECT);
    g.DrawRect(border, mRECT, nullptr, 1.5f);

    if (!valid) {
      g.DrawText(IText(14, IColor(255, 80, 80, 100)), "—", mRECT);
      return;
    }

    const auto& p = phrases[mIndex];
    std::string line1 = p.key + " " + p.mode + " " + p.phrase_type;
    std::string line2 = std::to_string(p.bars) + " bars · " + std::to_string(p.tempo_bpm) + " bpm";
    std::string line3 = p.description.substr(0, 50);

    IRECT r1 = mRECT.GetFromTop(mRECT.H() * 0.35f);
    IRECT r2 = mRECT.GetMidVPadded(mRECT.H() * 0.15f);
    IRECT r3 = mRECT.GetFromBottom(mRECT.H() * 0.35f);

    g.DrawText(IText(13, IColor(255, 200, 180, 255), "Roboto-Bold"), line1.c_str(), r1);
    g.DrawText(IText(11, IColor(255, 140, 140, 160)), line2.c_str(), r2);
    g.DrawText(IText(10, IColor(255, 100, 100, 120), "Roboto", EAlign::Near),
               line3.c_str(), r3);

    if (mMouseOver)
      g.DrawText(IText(10, IColor(255, 180, 160, 255)), "⟵ drag to DAW",
                 mRECT.GetFromBottom(16.f));
  }

  void OnMouseDown(float x, float y, const IMouseMod& mod) override {
    if (mIndex < (int)mPlug->GetGeneratedPhrases().size())
      mPlug->BeginDragOut(mIndex, x, y);
  }

  void OnMouseOver(float x, float y, const IMouseMod& mod) override {
    mMouseOver = true; SetDirty(false);
  }
  void OnMouseOut() override { mMouseOver = false; SetDirty(false); }

private:
  LinkVST* mPlug;
  int mIndex;
  bool mMouseOver = false;
};


// ---------------------------------------------------------------------------
// GeneratePanel — wired into LinkVST::OnUIOpen()
// ---------------------------------------------------------------------------
void BuildGenerateUI(IGraphics* g, LinkVST* plug) {
  constexpr int W = 600, H = 440;

  g->AttachBackground(IColor(255, 14, 15, 22));

  // -- Generate button --
  IRECT btnRect(10, 8, 120, 36);
  g->AttachControl(new ILambdaControl(btnRect,
    [plug](IControl* c, IGraphics& g) {
      IColor bg = c->IsMouseOver() ? IColor(255, 100, 80, 200) : IColor(255, 70, 55, 160);
      g.FillRoundRect(bg, c->GetRECT(), 4.f);
      g.DrawText(IText(13, IColor(255, 240, 240, 255), "Roboto-Bold"), "▶  Generate", c->GetRECT());
    },
    kNoValIdx,
    [plug](IControl* c, const IMouseMod& mod) {
      plug->RequestGenerate(4);
    }
  ));

  // -- Phrase tiles (2×2 grid) --
  float tw = (W - 30.f) / 2.f, th = (H - 120.f) / 2.f;
  for (int i = 0; i < 4; i++) {
    float col = i % 2, row = i / 2;
    IRECT tile(10.f + col * (tw + 10.f),
               50.f + row * (th + 10.f),
               10.f + col * (tw + 10.f) + tw,
               50.f + row * (th + 10.f) + th);
    g->AttachControl(new PhraseTile(plug, i, tile));
  }

  // -- Status bar --
  IRECT statusRect(10, H - 60, W - 10, H - 40);
  g->AttachControl(new ILambdaControl(statusRect,
    [plug](IControl* c, IGraphics& g) {
      g.DrawText(IText(11, IColor(255, 120, 120, 140)), plug->GetStatusMessage().c_str(), c->GetRECT());
    }
  ));

  // -- Upload MIDI button --
  IRECT uploadRect(W - 130, H - 36, W - 10, H - 10);
  g->AttachControl(new ILambdaControl(uploadRect,
    [](IControl* c, IGraphics& g) {
      g.FillRoundRect(IColor(255, 40, 40, 60), c->GetRECT(), 4.f);
      g.DrawText(IText(12, IColor(255, 160, 160, 180)), "Upload MIDI", c->GetRECT());
    },
    kNoValIdx,
    [plug](IControl* c, const IMouseMod& mod) {
      // Open file dialog — platform-specific
      WDL_String path;
      if (c->GetUI()->PromptForFile(path, EFileAction::Open, "mid midi")) {
        plug->UploadMidiFile(path.Get());
      }
    }
  ));
}
