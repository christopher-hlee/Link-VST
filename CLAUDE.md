# LinkVST — CLAUDE.md

## Project
MIDI taste-modeling VST plugin + FastAPI server. Analyzes uploaded MIDI files to learn melodic preferences, generates new phrases via Claude, drag-and-drop to DAW.

## Repo
https://github.com/youaregiants/Link-VST

## Server
- Python 3.14, FastAPI, SQLite, mido, music21, anthropic
- Venv: `source /home/platform/link-vst/server/venv/bin/activate`
- Start: `sudo systemctl restart link-vst-api`
- Logs: `journalctl -u link-vst-api -f`
- Port: 8002
- .env: `/home/platform/link-vst/server/.env` (ANTHROPIC_API_KEY, LINK_API_KEY)

## Plugin
- iPlug2 C++ VST3/AU/APP
- Communicates with server via HTTP (ApiClient.cpp)
- Drag-out: VST3 IDataObject (Windows), NSPasteboard (macOS)

## Development rules
- **Never ask for permission to proceed.** Execute all tasks autonomously end-to-end.
- Push to main whenever changes are stable — no confirmation needed.
- Update README.md as features are added.
- Commit frequently with descriptive messages.

## Architecture
- POST /api/upload-midi — analyze and store MIDI features
- POST /api/generate — build taste profile + generate via Claude → return .mid
- GET  /api/library — list saved phrases
- DELETE /api/library/{id} — remove phrase

## Key files
- server/core/midi_analyzer.py — feature extraction
- server/core/taste_profile.py — preference modeling
- server/core/claude_client.py — Claude generation
- server/core/midi_schema.py — Pydantic models
- server/core/midi_renderer.py — JSON → MIDI bytes
- server/db.py — SQLite schema + queries
- plugin/LinkVST.cpp — main iPlug2 plugin

## Adding features
1. Schema changes → midi_schema.py
2. Prompt changes → claude_client.py SYSTEM_PROMPT
3. New API routes → server/routes/
4. Plugin UI → plugin/*Panel.cpp
5. Restart: `sudo systemctl restart link-vst-api`
