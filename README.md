# LinkVST

A MIDI taste-modeling VST3/AU plugin that learns your melodic preferences from uploaded MIDI files, then generates new chord progressions, melodies, arpeggios, and basslines via Claude — all drag-and-drop ready.

## How it works

1. **Upload** your personal MIDI collection (chord progressions, melodies, arps, whatever you make)
2. **Analyze** — the server extracts musical features: key, mode, note density, rhythmic feel, melodic contour, chord complexity
3. **Learn** — as you upload more files, a taste profile builds from your patterns
4. **Generate** — Claude receives your taste profile as context and produces 4 new phrases tuned to your style
5. **Drag out** — drag any phrase tile directly onto your DAW timeline as a `.mid` file

Your library (both uploads and saved generations) persists across sessions in a local SQLite database.

## Architecture

```
server/               FastAPI + Claude + SQLite
  core/
    midi_analyzer.py  Feature extraction (key detection, contour, density)
    taste_profile.py  Aggregates features into a preference model
    claude_client.py  Prompts Claude with few-shot exemplars from your library
    midi_schema.py    Pydantic models: NoteEvent, GeneratedPhrase, MidiFeatures
    midi_renderer.py  JSON note arrays → .mid bytes via mido
  routes/
    upload.py         POST /api/upload-midi
    generate.py       POST /api/generate
    library.py        GET/DELETE /api/library
  db.py               SQLite schema + queries

plugin/               iPlug2 C++ VST3/AU/APP
  LinkVST.h/.cpp      Main plugin + drag-out logic
  GeneratePanel.cpp   UI: generate button, phrase tiles, upload button
  ApiClient.h/.cpp    HTTP client to server
  CMakeLists.txt      Build config
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/upload-midi` | Upload a `.mid` file; returns extracted features |
| `POST` | `/api/generate` | Generate phrases using taste profile |
| `GET`  | `/api/library` | List saved phrases |
| `GET`  | `/api/library/{id}/midi` | Download phrase as base64 MIDI |
| `DELETE` | `/api/library/{id}` | Remove from library |

### Generate request body
```json
{
  "count": 4,
  "phrase_type": null,
  "key": null,
  "mode": null,
  "bars": 4,
  "hint": "",
  "variety": true
}
```

## Server setup

```bash
# Clone and deploy
git clone https://github.com/youaregiants/Link-VST.git
cd Link-VST
cp server/.env.example server/.env
# Edit server/.env — add ANTHROPIC_API_KEY and LINK_API_KEY
chmod +x deploy.sh && ./deploy.sh
```

Health check: `curl http://localhost:8002/health`

## Plugin build

Requires [iPlug2](https://github.com/iPlug2/iPlug2).

```bash
cd plugin
cmake -B build -DIPLUG2_PATH=/path/to/iPlug2
cmake --build build --config Release
```

## Roadmap

- [ ] Plugin UI polish (waveform preview, key/type filter dropdowns)
- [ ] MIDI preview playback in-plugin before drag-out
- [ ] Library browser panel with search/filter
- [ ] Style presets (e.g. "jazz", "lo-fi", "cinematic")
- [ ] Export all library as zip
- [ ] Humanization controls (swing, velocity variance)
