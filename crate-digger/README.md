# Crate Digger

A local-first crate-digging studio for sample-based beatmaking.

Search public-domain and openly-licensed audio archives, audition records the
way you would flip through a stack, chop what you keep, and drag the pieces
straight into your DAW. Everything lives on your own machine — the only thing
that leaves it is the search itself.

Built for the J Dilla / Nujabes end of things: dusty 78s, piano trios, gospel
choirs, string beds, spoken-word interludes and drum breaks.

<!-- screenshot: the crate view with a loop region set and a kit chopped -->

---

## Why this exists

The hard part of making beats like that isn't the drums, it's finding the
record. Crate Digger points at the archives that are genuinely open — the
Internet Archive's Great 78 Project alone is hundreds of thousands of
digitised shellac transfers — and gives you a fast way to rummage through
them.

Then it does the boring parts: works out the tempo and key, finds the
transients, cuts the loop, and hands the file to your DAW.

## What it does

- **Dig** — 13 preset seams into the archives, each landing on a *random* page,
  because page one is the records everyone already has. Audition with the
  keyboard: <kbd>Space</kbd> to play, <kbd>S</kbd> to keep, <kbd>X</kbd> to
  pass. Anything you pass on never comes back.
- **Search** — query the Internet Archive and Openverse directly, filtered by
  year, collection and subject. Discogs is wired in for crate *leads* — label,
  year, style — when you want to go looking for something specific.
- **Analyse** — every record you keep gets a tempo, a key (with its Camelot
  code), loudness, a waveform, and a **breakiness** score: how drum-forward it
  is, so you can sort your crate by "where are the breaks".
- **Chop** — slice at the hits or on a musical grid. Cuts land on zero
  crossings with micro-fades, so one-shots never click. Export writes a
  numbered kit ready to drop on a drum rack.
- **Loop** — drag a region on the waveform and render it. Pull it to your
  project tempo with **varispeed** — pitch and speed move together, the way
  they do on a turntable, which is the sound you're after anyway.
- **Get it out** — drag a record, a loop, or any pad straight from the browser
  into Ableton, FL, Logic or Finder. Or push everything to a watch folder your
  DAW browser already points at.
- **YouTube** — paste a URL and it's filed as a lead with its title, channel
  and the `?t=` timestamp you liked, so you can go get it. Optionally, turn on
  the local ripper and it will fetch the audio with `yt-dlp` for you.

## Install

Python 3.10+. No ffmpeg needed — libsndfile reads MP3, FLAC, OGG and WAV.

```bash
git clone https://github.com/christopher-hlee/crate-digger.git
cd crate-digger
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Run

```bash
crate serve
# → http://127.0.0.1:8770
```

Point it at a folder your DAW watches and everything you chop shows up there:

```bash
export CRATE_LIBRARY_DIR=~/CrateDigger
export CRATE_EXPORT_DIR="~/Music/Ableton/User Library/Samples/Crate"
crate serve
```

See [`.env.example`](.env.example) for every setting, and
[`docs/DAW_WORKFLOW.md`](docs/DAW_WORKFLOW.md) for wiring it into Ableton, FL,
Logic and Maschine.

## The seams

| Dig | What's down there |
|---|---|
| `dusty-78s` | Pre-war jazz and blues off shellac. Surface noise included. |
| `soul-45s` | Pre-1972 soul, funk and R&B — horn stabs, tambourines, tape hiss. |
| `piano-trios` | Piano, upright bass, brushed drums. The Nujabes food group. |
| `bossa` | Nylon strings, soft snares, everything behind the beat. |
| `library-music` | Production music written to be used. Odd, cinematic, unclaimed. |
| `gospel` | Choirs, organ, room reverb. Chop the vocal, keep the room. |
| `strings` | Lush orchestral beds. Pitch them down and they glow. |
| `organ-rhodes` | Hammond, Wurlitzer, electric piano. |
| `breaks` | Percussion-forward records. Then sort by breakiness. |
| `spoken-word` | Interviews, sermons, old radio. Where interludes come from. |
| `netlabel-instrumentals` | Modern, Creative Commons, cleared for release. |
| `cc-jazz` / `cc-drums` | Openly licensed, safe to put out. |

Copy any of them in [`crate/digs.py`](crate/digs.py) and change the years or
subjects to make your own.

## Command line

```bash
crate digs                              # list the seams
crate dig soul-45s -v                   # rummage one (random page)
crate dig dusty-78s --pull 5            # …and pull the first five down
crate search "upright bass" --year-from 1960 --year-to 1972
crate import ~/Music/rips/take-01.wav
crate ls --bpm 80 100 --breaks 0.5      # 80-100 BPM, drum-forward
crate chop 12 --grid 4 --export         # one bar per slice, write WAVs
crate loop 12 8.0 15.2 --bpm 88         # render a loop at 88
crate analyze 12
```

## Where the audio comes from

Every wired-in source is one that comes with an answer about reuse already
attached — public domain, or an explicit open licence. Each record carries its
licence through to your crate and shows it on the card.

**The exception is YouTube**, which is a bookmark feature: pasting a URL files
the video's title and channel as a lead, and nothing more. There is an
optional local `yt-dlp` bridge, off by default, for when you're entitled to the
audio. What you do with the result is your call to make —
[`docs/SOURCES.md`](docs/SOURCES.md) lays out what each source actually
permits, because "found it on the internet" has never been a licence.

## How it works

```
crate/
  digs.py            preset seams into the archives
  library.py         download → analyse → file it
  db.py              SQLite: samples, crates, markers, slices, verdicts
  jobs.py            background download/analysis queue
  ripper.py          optional yt-dlp bridge (off by default)
  audio/
    decode.py        libsndfile loading, linear resample (= varispeed)
    dsp.py           onsets, tempo, key, HPSS, loudness, peaks
    chop.py          slicing, fades, loop rendering, WAV export
    cache.py         keeps recent decodes in memory so chopping feels live
  sources/
    internet_archive.py   the deep crate
    openverse.py          FMA / Jamendo / ccMixter, openly licensed
    youtube.py            leads only, via public oEmbed
    discogs.py            metadata leads, no audio
  server/            FastAPI + a vanilla-JS front end
```

The analysis is plain numpy and scipy — no librosa, no numba, no compile step.
Tempo is autocorrelation over a spectral-flux onset envelope with a log-normal
prior and half/double-time correction; key is Krumhansl-Kessler profile
matching over a chroma vector; breakiness is the percussive share of energy
after a median-filter harmonic/percussive split.

**On tempo:** autocorrelation genuinely cannot tell 82 BPM from 165 on some
material — both explain the same peaks. When it lands on the wrong octave, the
`×2` button on the BPM badge fixes it in one click.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

131 tests: DSP against synthetic signals with known tempo and key, chopping and
varispeed maths, the SQLite layer, every source adapter against mocked HTTP,
and the whole API end to end.

## Roadmap

- [ ] A VST3/AU build so the crate lives inside the DAW — the HTTP API here is
      already the one a plugin would talk to (same shape as
      [LinkVST](https://github.com/christopher-hlee/Link-VST)'s `ApiClient`).
- [ ] "More like this" — find records sharing a label, year or arranger.
- [ ] Auto-detect the best 4 bars: highest breakiness, cleanest loop point.
- [ ] Stem separation, so you can lift the drums out from under the horns.

## Licence

MIT — see [LICENSE](LICENSE). The licence covers this code, not the records you
pull with it.
