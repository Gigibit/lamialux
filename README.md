# LamiaLux

A Django app that requests an audiobook from Storytel and streams its audio source into Daydream Canvas to generate AI video output.

## Features
- Ask for a book title/query.
- Fetch audiobook stream source via configurable Storytel endpoint.
- Start a Daydream canvas stream using Stable Diffusion settings.
- Show resulting output video.
- Update Daydream prompt below the video (defaults to `go with the flow`).

## Configuration
Copy `.env.example` to `.env` and fill all values:

```bash
cp .env.example .env
```

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000

The Daydream defaults in `.env.example` target the current API host and endpoints:

- `DAYDREAM_BASE_URL=https://api.daydream.live`
- `DAYDREAM_CANVAS_STREAM_PATH=/v1/streams`
- `DAYDREAM_PROMPT_UPDATE_PATH_TEMPLATE=/v1/streams/{session_id}`

Older values such as `https://app.daydream.live` and `/api/canvas/streams` now return 404s for stream creation.

## Quality checks
```bash
ruff check .
python manage.py test
```

## Narrative modes
- `THIRDY_PARTS_STORYTEL`: preserves the original Storytel-to-Daydream streaming behavior.
- `WEB_RESEARCH_TTS`: searches for a PDF on the web, extracts chapter chunks, stores them into a local SQLite database, and prepares synchronized browser TTS + Daydream prompt playback.

Set the mode in `.env`:

```bash
NARRATIVE_MODE_PROVIDER=THIRDY_PARTS_STORYTEL
```
