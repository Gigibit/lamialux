# LamiaLux

A Django app that finds audiobook sources, prepares playable media, and streams their audio source into Daydream Canvas to generate AI video output.

## Features
- Ask for a book title/query.
- Find audiobook sources via web research or OpenAI web search.
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
- `THIRDY_PARTS_STORYTEL`: currently aliases to the PDF/web research flow for backward compatibility.
- `WEB_RESEARCH_TTS`: searches for a PDF on the web, extracts chapter chunks, stores them into a local SQLite database, and prepares synchronized browser TTS + Daydream prompt playback.
- `OPENAI_SEARCH`: uses the OpenAI Responses API with web search to find an audiobook video/media URL, downloads that source, and reuses its media audio directly instead of generating Coqui narration.

Set the mode in `.env`:

```bash
NARRATIVE_MODE_PROVIDER=THIRDY_PARTS_STORYTEL
```

## OpenAI search mode

Set `NARRATIVE_MODE_PROVIDER=OPENAI_SEARCH` and configure `OPENAI_API_KEY` to let the app look up an audiobook media URL via OpenAI web search, cache the downloaded media in `storage/openai_search_media`, and feed that source into the browser audio player.
