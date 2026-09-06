# Getting it into the DAW

Three routes out. Use whichever fits how you already work.

## 1. Drag straight from the browser

The fastest one. Grab the `⠿` handle — on the whole record, on a rendered loop,
or on any chop pad — and drag it into your DAW's arrangement or browser.

This uses Chromium's `DownloadURL` drag type, which hands a real file to the
operating system mid-drag. It works in **Chrome, Edge, Brave and Arc**.

**Firefox and Safari don't implement it.** They'll show the drag handle greyed
out in intent and the note under it says so; use route 2 or 3 there instead.

Targets that accept a dragged file: Ableton Live (arrangement or Session slot),
FL Studio (playlist or Channel Rack), Logic Pro (tracks area), Reaper, Bitwig,
Maschine's browser, and Finder/Explorer.

## 2. The watch folder

The universal one. Point Crate Digger at a folder your DAW browser already
watches, and everything you export lands there for you to pull in normally:

```bash
export CRATE_EXPORT_DIR="~/Music/Ableton/User Library/Samples/Crate"
```

Then tick **→ DAW folder** when you render a loop or export a kit, or press
**Send to DAW folder** on any record.

Where that folder usually is:

| DAW | Folder |
|---|---|
| Ableton Live | `~/Music/Ableton/User Library/Samples/` |
| FL Studio | `~/Documents/Image-Line/FL Studio/Presets/` (or add any folder in Settings → File) |
| Logic Pro | `~/Music/Audio Music Apps/Samples/` |
| Maschine / Kontakt | add the folder under Preferences → Library → User |
| Reaper / Bitwig | any folder, added to the media browser |

Ableton and Maschine index new files automatically; Logic and FL may want a
browser refresh.

## 3. Save

Every render and pad has a plain **Save** link. Ordinary download, goes
wherever your browser puts things.

---

## Working a record

1. **Dig** until something makes you stop, then press <kbd>S</kbd>. It
   downloads and analyses in the background while you keep going.
2. Open it from the **Crate**. Check the badges — tempo, key, and the `% drums`
   score telling you how percussive it is.
3. **Find the loop.** Drag across the waveform. The label under the transport
   reads the region back in bars, so you can see when you've got a clean 2 or 4.
4. **Pull it to tempo.** Type your project's BPM into *To BPM* and hit
   **Render loop**. Varispeed moves pitch and speed together — the semitone
   shift is printed next to the file, which is what you need to know before you
   play keys over it.
5. **Or chop it.** *At the hits* follows the transients; *On the grid* cuts
   even bars or beats. **Preview** draws the cuts on the waveform for free;
   **Export kit** writes the WAVs.
6. **Drag it out.**

## Notes on the tempo reading

Autocorrelation can't always tell 82 BPM from 165 — both explain the same
peaks. If the number looks like the wrong octave, hit `×2` on the BPM badge; it
halves or doubles and re-saves. Grid chopping and loop varispeed both use that
stored value, so fix it before you cut.

`% drums` is the percussive share of the signal after a harmonic/percussive
split. Above ~55% you're looking at a break; in the single digits you've got a
pad or a string bed. Sort your whole crate by it with **Most drums**.

## Cut quality

Chop points are snapped to the nearest zero crossing before the transient,
with a 3 ms fade on both edges. That's why the one-shots don't click — you can
load a whole kit onto pads without cleaning anything up first.
