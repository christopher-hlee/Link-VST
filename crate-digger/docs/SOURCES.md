# Where the audio comes from

Every source wired into Crate Digger was picked because it comes with an answer
about reuse already attached. The licence travels with the record: it's stored
on the row, shown on the card, and written into the crate.

That answer still isn't legal advice, and it isn't a clearance. Sampling law is
messy, jurisdictional, and turns on facts about a specific recording. Read this
as "here is what each source claims about itself", and check anything you plan
to release.

---

## Internet Archive — `ia`

The deep crate. Search runs over `mediatype:audio`; an item expands into its
tracks, preferring FLAC over MP3 and skipping the 30-second preview
derivatives.

| Collection | What it is | Status |
|---|---|---|
| `georgeblood` | **The Great 78 Project** — hundreds of thousands of 78rpm transfers | Recordings out of US copyright; transfers released openly |
| `78rpm` | Other shellac digitisations | Usually public domain, varies by item |
| `unlockedrecordings` | Pre-1972 recordings opened up under the Music Modernization Act | Opened for non-commercial use — **check before you release** |
| `netlabels` | Modern netlabel releases | Creative Commons, per item |
| `oldtimeradio` | Broadcast radio, sermons, interviews | Mostly public domain |
| `audio_music` | General music uploads | **Varies wildly** — trust the item's own licence, not the collection |

The `licenseurl` on each item is what the uploader asserted. `audio_music` is
a general dumping ground and includes uploads whose rights were never the
uploader's to give. Treat a bare `audio_music` hit with more suspicion than a
`georgeblood` one.

**US public domain:** as of 2026, sound recordings first published in 1930 or
earlier are in the US public domain, and the window moves forward each year
under the Music Modernization Act. That is what makes the 78s seam legitimately
open — and why the year filters on that dig are set where they are.

## Openverse — `openverse`

One API over Free Music Archive, Jamendo, ccMixter and Wikimedia Commons.
Crate Digger asks for `license_type=commercial,modification`, which is the
subset you can both sell and alter — the set that matters for a release.

Nearly all of it is Creative Commons, so **attribution is usually required**,
and `SA` variants require you to license the result the same way. The full
licence string and its URL land on every row.

ccMixter in particular exists to be sampled — it's the friendliest corner here.

## Discogs — `discogs`

**No audio, by design.** Discogs is the reference book: which label put out
which record in which year, and what everyone calls the style. Use it to decide
what to go hunting for, then search the Archive for it. Needs a free personal
token (`CRATE_DISCOGS_TOKEN`); dormant without one.

## YouTube — `youtube`

**A bookmark feature.** Pasting a URL calls YouTube's public oEmbed endpoint —
a documented, keyless endpoint that returns a title, channel and thumbnail —
and files that as a lead. A `?t=` timestamp becomes a marker so you don't lose
the spot. No audio URL is resolved, and nothing is scraped.

### The optional ripper

`crate/ripper.py` shells out to `yt-dlp` for a single URL, on your machine,
when you ask it to. It is **off unless you turn it on**:

```bash
pip install yt-dlp
export CRATE_ENABLE_RIPPER=true
```

It is a convenience wrapper around the command you would otherwise type — one
URL at a time, `--no-playlist`, no crawling, no bulk queue. It exists because
you said you'd be ripping by hand anyway.

Whether you may extract a given video's audio depends on the video, the rights
holder, your jurisdiction and what you do next. Sampling a copyrighted
recording without clearance is infringement in most places regardless of how
short the sample is or how much you changed it — the "under X seconds is fine"
rule of thumb is folklore, not law. The archives above exist precisely so that
most of what you pull doesn't need this. Use it accordingly.

## Local files — `local`

`crate import <path>` brings in audio you already have — your own rips, your
own records, stems you made. Copied into the library by default; pass
`--no-copy` to index in place. Licensed as "Local file", i.e. you already know.

---

## Practical rules of thumb

1. **Releasing it?** Stay in `georgeblood`, `netlabels` and the Openverse
   digs (`cc-jazz`, `cc-drums`). Those come with an answer.
2. **Just practising?** Dig anywhere. Nobody is coming for your unreleased
   beat folder.
3. **Attribution is cheap.** The artist, year and licence are on every row —
   copy them into your release notes and most CC obligations are met.
4. **`unlockedrecordings` is not a blanket yes.** It opened pre-1972 recordings
   for *non-commercial* use. Check the specific item.
5. **When you don't know, you don't know.** A licence field that's empty means
   nobody asserted anything — not that it's free.
