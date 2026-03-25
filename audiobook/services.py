from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import YouTubeTranscriptApiException

logger = logging.getLogger("audiobook")


def _http_response_debug_context(response: httpx.Response) -> dict[str, object]:
    return {
        "status": response.status_code,
        "url": str(response.request.url) if response.request else "",
        "content_type": response.headers.get("content-type"),
        "body_preview": response.text[:400],
    }


class UpstreamServiceError(Exception):
    """Raised when an upstream provider returns an error."""


@dataclass
class NarrativeChunk:
    chapter_title: str
    chunk_index: int
    text: str


@dataclass
class NarrativeExperience:
    title: str
    author: str
    pdf_url: str
    chunks: list[NarrativeChunk]
    storage_path: str
    cache_hit: bool = False
    source_audio_path: str = ""
    source_audio_mime_type: str = ""
    source_video_url: str = ""


@dataclass
class MusicExperience:
    title: str
    artist: str
    album: str
    cover_image_url: str
    external_url: str
    preview_url: str
    spotify_uri: str
    provider: str


@dataclass
class CoquiTtsResult:
    audio_path: str
    mime_type: str
    cache_hit: bool = False


@dataclass
class StreamSession:
    session_id: str
    whip_url: str
    whep_url: str
    output_video_url: str
    upstream_stream_id: str = ""
    initial_whep_url: str = ""
    whep_resource_url: str = ""


class OpenAiSearchNarrativeClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.getenv("OPENAI_SEARCH_MODEL", "gpt-4.1-mini")
        self.timeout = float(os.getenv("OPENAI_SEARCH_TIMEOUT_SECONDS", "45"))
        self.media_cache_dir = Path(
            os.getenv("OPENAI_SEARCH_MEDIA_CACHE_DIR", "storage/openai_search_media")
        )

    def build_experience(self, query: str) -> NarrativeExperience:
        if not self.api_key:
            logger.error("OPENAI_SEARCH mode requested without OPENAI_API_KEY configured.")
            raise UpstreamServiceError("OPENAI_SEARCH requires OPENAI_API_KEY.")

        logger.info("OpenAI narrative experience requested for query='%s'.", query)
        search_result = self._search_audiobook_video(query)
        media_result = self._download_media(search_result["video_url"])
        logger.info(
            "OpenAI narrative experience prepared for query='%s' with media cache_hit=%s path=%s",
            query,
            media_result.cache_hit,
            media_result.audio_path,
        )
        return NarrativeExperience(
            title=search_result["title"],
            author=search_result["author"],
            pdf_url=search_result["video_url"],
            chunks=[
                NarrativeChunk(
                    chapter_title="Audiobook source",
                    chunk_index=1,
                    text=search_result["summary"],
                )
            ],
            storage_path=str(self.media_cache_dir),
            cache_hit=media_result.cache_hit,
            source_audio_path=media_result.audio_path,
            source_audio_mime_type=media_result.mime_type,
        )

    def _search_audiobook_video(self, query: str) -> dict[str, str]:
        prompt = (
            "Find one publicly reachable audiobook video page or direct media URL "
            "for the requested work. Prefer stable video hosts that allow "
            "direct playback in a browser. Respond with ONLY valid JSON "
            "matching this schema: "
            '{"title": string, "author": string, "video_url": string, "summary": string}. '
            f"User request: {query}"
        )
        payload = {
            "model": self.model,
            "input": prompt,
            "tools": [{"type": "web_search_preview"}],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            try:
                logger.info(
                    (
                        "Calling OpenAI responses API for audiobook "
                        "query='%s' model='%s' base_url='%s'."
                    ),
                    query,
                    self.model,
                    self.base_url,
                )
                response = client.post(f"{self.base_url}/responses", headers=headers, json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "OpenAI search failed with status %s for query '%s': %s",
                    exc.response.status_code,
                    query,
                    exc,
                )
                raise UpstreamServiceError(
                    "OpenAI search could not find an audiobook source."
                ) from exc
            except httpx.HTTPError as exc:
                logger.error("OpenAI search connection error for query '%s': %s", query, exc)
                raise UpstreamServiceError("OpenAI search is unavailable right now.") from exc

        logger.info(
            "OpenAI responses API returned successful search payload for query='%s': %s",
            query,
            _http_response_debug_context(response),
        )
        parsed = self._parse_openai_search_response(response.json())
        video_url = str(parsed.get("video_url") or "").strip()
        if not video_url.startswith(("http://", "https://")):
            logger.error(
                "OpenAI search returned an invalid video_url for query '%s': %s", query, parsed
            )
            raise UpstreamServiceError("OpenAI search did not return a usable audiobook video URL.")
        return {
            "title": str(parsed.get("title") or query).strip() or query,
            "author": str(parsed.get("author") or "Unknown author").strip() or "Unknown author",
            "video_url": video_url,
            "summary": str(parsed.get("summary") or f"Streaming audio for {query}.").strip()
            or f"Streaming audio for {query}.",
        }

    def _parse_openai_search_response(self, payload: dict[str, object]) -> dict[str, object]:
        text_fragments: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text_value = str(content.get("text") or "").strip()
                    if text_value:
                        text_fragments.append(text_value)
        aggregated_text = "\n".join(text_fragments).strip()
        if not aggregated_text:
            logger.error("OpenAI search response did not contain output_text: %s", payload)
            raise UpstreamServiceError("OpenAI search returned an empty result.")
        try:
            return json.loads(aggregated_text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", aggregated_text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError as exc:
                    logger.error(
                        "OpenAI search returned unparsable JSON content: %s error=%s",
                        aggregated_text[:1000],
                        exc,
                    )
                    raise UpstreamServiceError(
                        "OpenAI search returned an invalid audiobook result."
                    ) from exc
            logger.error("OpenAI search returned non-JSON content: %s", aggregated_text[:1000])
            raise UpstreamServiceError("OpenAI search returned an invalid audiobook result.")

    def _download_media(self, media_url: str) -> CoquiTtsResult:
        self.media_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(media_url.encode("utf-8")).hexdigest()
        existing = next(iter(sorted(self.media_cache_dir.glob(f"{cache_key}.*"))), None)
        if existing is not None:
            logger.info(
                "Using cached OpenAI media download for url='%s' at '%s'.",
                media_url,
                existing,
            )
            return CoquiTtsResult(
                audio_path=str(existing),
                mime_type=self._guess_media_mime_type(existing.suffix),
                cache_hit=True,
            )

        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            try:
                logger.info("Downloading audiobook media from '%s'.", media_url)
                response = client.get(media_url, headers={"User-Agent": "Mozilla/5.0 LamiaLux/1.0"})
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Audiobook media download failed with status %s for %s: %s",
                    exc.response.status_code,
                    media_url,
                    exc,
                )
                raise UpstreamServiceError(
                    "The selected audiobook media could not be downloaded."
                ) from exc
            except httpx.HTTPError as exc:
                logger.error("Audiobook media download connection error for %s: %s", media_url, exc)
                raise UpstreamServiceError("Audiobook media download is unavailable.") from exc

        logger.info(
            "Downloaded audiobook media response: %s",
            _http_response_debug_context(response),
        )
        content_type = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
        suffix = self._suffix_for_media_url(media_url, content_type)
        output_path = self.media_cache_dir / f"{cache_key}{suffix}"
        output_path.write_bytes(response.content)
        mime_type = content_type or self._guess_media_mime_type(suffix)
        return CoquiTtsResult(audio_path=str(output_path), mime_type=mime_type, cache_hit=False)

    def _suffix_for_media_url(self, media_url: str, content_type: str) -> str:
        parsed = urlparse(media_url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix in {".mp4", ".m4a", ".mp3", ".webm", ".ogg", ".wav"}:
            return suffix
        if content_type == "video/mp4":
            return ".mp4"
        if content_type == "audio/mpeg":
            return ".mp3"
        if content_type == "audio/mp4":
            return ".m4a"
        if content_type == "video/webm":
            return ".webm"
        if content_type == "audio/ogg":
            return ".ogg"
        logger.error(
            "Unknown audiobook media suffix/content-type combination. "
            "media_url=%s content_type=%s; defaulting to .mp4",
            media_url,
            content_type,
        )
        return ".mp4"

    def _guess_media_mime_type(self, suffix: str) -> str:
        return {
            ".mp4": "video/mp4",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".webm": "video/webm",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
        }.get(suffix.lower(), "application/octet-stream")


class WebResearchNarrativeClient:
    def __init__(self) -> None:
        self.timeout = float(os.getenv("WEB_RESEARCH_TIMEOUT_SECONDS", "20"))
        self.chunk_size = int(os.getenv("WEB_RESEARCH_CHUNK_SIZE", "650"))
        self.sqlite_path = Path(
            os.getenv("WEB_RESEARCH_SQLITE_PATH", "storage/web_research_chunks.sqlite3")
        )
        self.search_url = os.getenv("WEB_RESEARCH_SEARCH_URL", "https://duckduckgo.com/html/")

    def build_experience(self, query: str) -> NarrativeExperience:
        logger.info("Web research narrative experience requested for query='%s'.", query)
        cached_experience = self._load_cached_experience(query)
        if cached_experience is not None:
            logger.info("Using cached narrative chunks for query '%s'.", query)
            return cached_experience

        pdf_url = self._search_pdf(query)
        pdf_bytes = self._download_pdf(pdf_url)
        chunks = self._extract_chunks(pdf_bytes)
        if not chunks:
            logger.error(
                "No chunks extracted from PDF for query '%s' using pdf %s.",
                query,
                pdf_url,
            )
            raise UpstreamServiceError("Unable to read usable text from the PDF.")

        self._store_chunks(query=query, pdf_url=pdf_url, chunks=chunks)
        return NarrativeExperience(
            title=self._derive_title(query, chunks),
            author="PDF source",
            pdf_url=pdf_url,
            chunks=chunks,
            storage_path=str(self.sqlite_path),
        )

    def _load_cached_experience(self, query: str) -> NarrativeExperience | None:
        if not self.sqlite_path.exists():
            return None

        with sqlite3.connect(self.sqlite_path) as connection:
            self._ensure_chunks_table(connection)
            rows = connection.execute(
                """
                SELECT pdf_url, chapter_title, chunk_index, text
                FROM narrative_chunks
                WHERE query = ?
                ORDER BY chapter_title, chunk_index, id
                """,
                (query,),
            ).fetchall()

        if not rows:
            return None

        pdf_url = str(rows[0][0])
        chunks = [
            NarrativeChunk(
                chapter_title=str(chapter_title),
                chunk_index=int(chunk_index),
                text=str(text),
            )
            for _, chapter_title, chunk_index, text in rows
        ]
        return NarrativeExperience(
            title=self._derive_title(query, chunks),
            author="PDF source",
            pdf_url=pdf_url,
            chunks=chunks,
            storage_path=str(self.sqlite_path),
            cache_hit=True,
        )

    def _search_pdf(self, query: str) -> str:
        search_query = f"{query} filetype:pdf"
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            try:
                logger.info(
                    "Calling web search for PDF query='%s' with search_url='%s'.",
                    search_query,
                    self.search_url,
                )
                response = client.get(
                    self.search_url,
                    params={"q": search_query},
                    headers={"User-Agent": "Mozilla/5.0 LamiaLux/1.0"},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Web PDF search failed with status %s for query '%s': %s",
                    exc.response.status_code,
                    query,
                    exc,
                )
                raise UpstreamServiceError("Web research search failed.") from exc
            except httpx.HTTPError as exc:
                logger.error("Web PDF search connection error for query '%s': %s", query, exc)
                raise UpstreamServiceError("Web research search is unavailable.") from exc

        logger.info(
            "Web PDF search response details for query='%s': %s",
            query,
            _http_response_debug_context(response),
        )
        for candidate in self._extract_pdf_candidates(response.text):
            normalized_url = self._normalize_pdf_url(candidate)
            if normalized_url:
                logger.info("Selected PDF candidate for query '%s': %s", query, normalized_url)
                return normalized_url

        logger.error("No PDF result found for query '%s'.", query)
        raise UpstreamServiceError("No PDF found on the web for this request.")

    def _extract_pdf_candidates(self, html: str) -> list[str]:
        patterns = [
            re.compile(
                r'nofollow" class="[^\"]*result__a[^\"]*" href="(?P<url>[^"]+)"',
                re.IGNORECASE,
            ),
            re.compile(r'href="(?P<url>[^"]+\.pdf[^"]*)"', re.IGNORECASE),
        ]
        candidates: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for match in pattern.finditer(html):
                candidate = unescape(match.group("url")).strip()
                if candidate and candidate not in seen:
                    candidates.append(candidate)
                    seen.add(candidate)
        return candidates

    def _normalize_pdf_url(self, candidate_url: str) -> str | None:
        parsed = urlparse(candidate_url)
        if parsed.scheme in {"http", "https"}:
            return candidate_url if ".pdf" in candidate_url.lower() else None
        if candidate_url.startswith("//duckduckgo.com/"):
            query_params = parse_qs(parsed.query)
            for key in ("uddg", "url", "target"):
                for value in query_params.get(key, []):
                    normalized_value = unescape(value).strip()
                    if (
                        normalized_value.lower().startswith(("http://", "https://"))
                        and ".pdf" in normalized_value.lower()
                    ):
                        return normalized_value
            logger.error("Discarding DuckDuckGo redirect without a PDF target: %s", candidate_url)
            return None
        if candidate_url.startswith("//"):
            normalized_url = f"https:{candidate_url}"
            return normalized_url if ".pdf" in normalized_url.lower() else None
        if parsed.netloc and not parsed.scheme:
            normalized_url = f"https://{candidate_url}"
            return normalized_url if ".pdf" in normalized_url.lower() else None
        if candidate_url.lower().endswith(".pdf") and self.search_url:
            return urljoin(self.search_url, candidate_url)
        logger.error("Discarding non-PDF or unsupported search candidate URL: %s", candidate_url)
        return None

    def _download_pdf(self, pdf_url: str) -> bytes:
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            try:
                logger.info("Downloading PDF content from '%s'.", pdf_url)
                response = client.get(pdf_url, headers={"User-Agent": "Mozilla/5.0 LamiaLux/1.0"})
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "PDF download failed with status %s for %s: %s",
                    exc.response.status_code,
                    pdf_url,
                    exc,
                )
                raise UpstreamServiceError("The selected PDF could not be downloaded.") from exc
            except httpx.HTTPError as exc:
                logger.error("PDF download connection error for %s: %s", pdf_url, exc)
                raise UpstreamServiceError("PDF download is unavailable.") from exc

        logger.info("Downloaded PDF response details: %s", _http_response_debug_context(response))
        return response.content

    def _extract_chunks(self, pdf_bytes: bytes) -> list[NarrativeChunk]:
        import io

        reader = PdfReader(io.BytesIO(pdf_bytes))
        text_parts: list[str] = []
        for page in reader.pages:
            extracted_text = (page.extract_text() or "").strip()
            if extracted_text:
                text_parts.append(extracted_text)

        normalized_text = re.sub(r"\n{2,}", "\n", "\n".join(text_parts))
        chapter_sections = self._split_chapters(normalized_text)
        chunks: list[NarrativeChunk] = []
        for chapter_title, chapter_text in chapter_sections:
            for chunk_index, piece in enumerate(self._chunk_text(chapter_text), start=1):
                chunks.append(
                    NarrativeChunk(
                        chapter_title=chapter_title,
                        chunk_index=chunk_index,
                        text=piece,
                    )
                )
        return chunks

    def _split_chapters(self, text: str) -> list[tuple[str, str]]:
        pattern = re.compile(
            r"(?im)^(chapter\s+[0-9ivxlcdm]+|prologue|epilogue|part\s+[0-9ivxlcdm]+)\b.*$"
        )
        matches = list(pattern.finditer(text))
        if not matches:
            return [("Narrative flow", text)] if text else []

        sections: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            chapter_title = match.group(0).strip().title()
            chapter_text = text[start:end].strip()
            if chapter_text:
                sections.append((chapter_title, chapter_text))
        return sections

    def _chunk_text(self, text: str) -> list[str]:
        words = text.split()
        if not words:
            return []

        pieces: list[str] = []
        current: list[str] = []
        current_length = 0
        for word in words:
            current.append(word)
            current_length += len(word) + 1
            if current_length >= self.chunk_size:
                pieces.append(" ".join(current))
                current = []
                current_length = 0
        if current:
            pieces.append(" ".join(current))
        return pieces

    def _derive_title(self, query: str, chunks: list[NarrativeChunk]) -> str:
        first_chapter = chunks[0].chapter_title if chunks else "Narrative flow"
        return f"{query} · {first_chapter}"

    def _store_chunks(self, query: str, pdf_url: str, chunks: list[NarrativeChunk]) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.sqlite_path) as connection:
            self._ensure_chunks_table(connection)
            connection.execute("DELETE FROM narrative_chunks WHERE query = ?", (query,))
            connection.executemany(
                """
                INSERT INTO narrative_chunks (
                    query, pdf_url, chapter_title, chunk_index, text
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        query,
                        pdf_url,
                        chunk.chapter_title,
                        chunk.chunk_index,
                        chunk.text,
                    )
                    for chunk in chunks
                ],
            )
            connection.commit()

    def _ensure_chunks_table(self, connection: sqlite3.Connection) -> None:
        table_columns = connection.execute("PRAGMA table_info(narrative_chunks)").fetchall()
        if not table_columns:
            connection.execute(
                """
                CREATE TABLE narrative_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    pdf_url TEXT NOT NULL,
                    chapter_title TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL
                )
                """
            )
            return

        column_names = {column[1] for column in table_columns}
        prompt_column = next((column for column in table_columns if column[1] == "prompt"), None)
        expected_columns = {"id", "query", "pdf_url", "chapter_title", "chunk_index", "text"}

        if prompt_column is not None and prompt_column[3]:
            logger.error(
                "Migrating legacy narrative_chunks table at %s to remove "
                "NOT NULL prompt dependency.",
                self.sqlite_path,
            )
            connection.executescript(
                """
                ALTER TABLE narrative_chunks RENAME TO narrative_chunks_legacy;
                CREATE TABLE narrative_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    pdf_url TEXT NOT NULL,
                    chapter_title TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL
                );
                INSERT INTO narrative_chunks (id, query, pdf_url, chapter_title, chunk_index, text)
                SELECT id, query, pdf_url, chapter_title, chunk_index, text
                FROM narrative_chunks_legacy;
                DROP TABLE narrative_chunks_legacy;
                """
            )
            return

        if not expected_columns.issubset(column_names):
            logger.error(
                "narrative_chunks table at %s has unsupported schema columns=%s.",
                self.sqlite_path,
                sorted(column_names),
            )
            raise UpstreamServiceError("Stored narrative chunk cache has an unsupported schema.")


class MusicSearchClient:
    SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
    SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"

    def __init__(self) -> None:
        self.provider = os.getenv("MUSIC_MODE_PROVIDER", "SPOTIFY").strip().upper()
        self.timeout = float(os.getenv("SPOTIFY_TIMEOUT_SECONDS", "20"))
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        self.max_retries = int(os.getenv("SPOTIFY_MAX_RETRIES", "3"))

    def search_track(self, query: str) -> MusicExperience:
        if self.provider != "SPOTIFY":
            logger.error("Unsupported MUSIC_MODE_PROVIDER '%s'.", self.provider)
            raise UpstreamServiceError("Only the SPOTIFY music provider is currently supported.")
        logger.info("Music search requested with provider='%s' query='%s'.", self.provider, query)
        access_token = self._fetch_spotify_access_token()
        return self._search_spotify_track(query=query, access_token=access_token)

    def _fetch_spotify_access_token(self) -> str:
        if not self.client_id or not self.client_secret:
            logger.error(
                (
                    "Spotify search requested without complete credentials. "
                    "client_id_present=%s client_secret_present=%s"
                ),
                bool(self.client_id),
                bool(self.client_secret),
            )
            raise UpstreamServiceError("Spotify credentials are not configured on the server.")

        encoded_credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        with httpx.Client(timeout=self.timeout) as client:
            try:
                logger.info("Requesting Spotify access token from '%s'.", self.SPOTIFY_TOKEN_URL)
                response = self._request_with_backoff(
                    client=client,
                    method="POST",
                    url=self.SPOTIFY_TOKEN_URL,
                    headers={
                        "Authorization": f"Basic {encoded_credentials}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"grant_type": "client_credentials"},
                )
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Spotify token request failed with status %s: %s",
                    exc.response.status_code,
                    exc,
                )
                raise UpstreamServiceError("Spotify authentication failed.") from exc
            except httpx.HTTPError as exc:
                logger.error("Spotify token request connection error: %s", exc)
                raise UpstreamServiceError("Spotify is unavailable right now.") from exc

        logger.info("Spotify token response details: %s", _http_response_debug_context(response))
        access_token = str(response.json().get("access_token") or "")
        if not access_token:
            logger.error(
                "Spotify token response did not include an access token: %s",
                response.text,
            )
            raise UpstreamServiceError("Spotify authentication returned incomplete data.")
        return access_token

    def _search_spotify_track(self, *, query: str, access_token: str) -> MusicExperience:
        with httpx.Client(timeout=self.timeout) as client:
            try:
                logger.info("Calling Spotify track search for query='%s'.", query)
                response = self._request_with_backoff(
                    client=client,
                    method="GET",
                    url=self.SPOTIFY_SEARCH_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"q": query, "type": "track", "limit": 1, "market": "US"},
                )
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Spotify track search failed with status %s for query '%s': %s",
                    exc.response.status_code,
                    query,
                    exc,
                )
                raise UpstreamServiceError("Spotify search failed.") from exc
            except httpx.HTTPError as exc:
                logger.error("Spotify track search connection error for query '%s': %s", query, exc)
                raise UpstreamServiceError("Spotify search is unavailable.") from exc

        logger.info(
            "Spotify track search response details for query='%s': %s",
            query,
            _http_response_debug_context(response),
        )
        items = response.json().get("tracks", {}).get("items", [])
        if not items:
            logger.error("Spotify returned no tracks for query '%s'.", query)
            raise UpstreamServiceError("No Spotify track was found for that title.")

        track = items[0]
        images = track.get("album", {}).get("images") or []
        artists = track.get("artists") or []
        return MusicExperience(
            title=str(track.get("name") or query),
            artist=", ".join(
                str(artist.get("name") or "") for artist in artists if artist.get("name")
            ),
            album=str(track.get("album", {}).get("name") or ""),
            cover_image_url=str(images[0].get("url") if images else ""),
            external_url=str(track.get("external_urls", {}).get("spotify") or ""),
            preview_url=str(track.get("preview_url") or ""),
            spotify_uri=str(track.get("uri") or ""),
            provider="SPOTIFY",
        )

    def _request_with_backoff(
        self, *, client: httpx.Client, method: str, url: str, **kwargs
    ) -> httpx.Response:
        attempt = 0
        while True:
            response = client.request(method=method, url=url, **kwargs)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            attempt += 1
            if attempt > self.max_retries:
                logger.error(
                    (
                        "Spotify request exceeded max retries after 429 responses. "
                        "method=%s url=%s max_retries=%s"
                    ),
                    method,
                    url,
                    self.max_retries,
                )
                response.raise_for_status()
            retry_after_raw = str(response.headers.get("Retry-After") or "").strip()
            retry_after_seconds = int(retry_after_raw) if retry_after_raw.isdigit() else 1
            backoff_seconds = max(retry_after_seconds, 2 ** (attempt - 1))
            logger.error(
                (
                    "Spotify rate limit encountered (429). Retrying with backoff. "
                    "method=%s url=%s attempt=%s sleep_seconds=%s"
                ),
                method,
                url,
                attempt,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
class NarrativeModeProvider:
    THIRDY_PARTS_STORYTEL = "THIRDY_PARTS_STORYTEL"
    WEB_RESEARCH_TTS = "WEB_RESEARCH_TTS"
    OPENAI_SEARCH = "OPENAI_SEARCH"
    YOUTUBE_SEARCH = "YOUTUBE_SEARCH"


class YouTubeSearchNarrativeClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        self.base_url = os.getenv("YOUTUBE_API_BASE_URL", "https://www.googleapis.com/youtube/v3")
        self.timeout = float(os.getenv("YOUTUBE_API_TIMEOUT_SECONDS", "20"))
        self.max_results = int(os.getenv("YOUTUBE_API_MAX_RESULTS", "5"))

    def build_experience(self, query: str) -> NarrativeExperience:
        if not self.api_key:
            logger.error("YOUTUBE_SEARCH mode requested without YOUTUBE_API_KEY configured.")
            raise UpstreamServiceError("YOUTUBE_SEARCH requires YOUTUBE_API_KEY.")

        logger.info("YouTube narrative experience requested for query='%s'.", query)
        result = self._search_video(query)
        return NarrativeExperience(
            title=result["title"],
            author=result["author"],
            pdf_url=result["video_url"],
            chunks=[
                NarrativeChunk(
                    chapter_title="YouTube audiobook source",
                    chunk_index=1,
                    text=result["summary"],
                )
            ],
            storage_path="youtube-search-api",
            source_video_url=result["video_url"],
        )

    def _search_video(self, query: str) -> dict[str, str]:
        normalized_query = f"{query} audiobook"
        params = {
            "part": "snippet",
            "q": normalized_query,
            "type": "video",
            "maxResults": str(self.max_results),
            "safeSearch": "strict",
            "key": self.api_key,
        }
        with httpx.Client(timeout=self.timeout) as client:
            try:
                logger.info(
                    "Calling YouTube search API for query='%s' max_results=%s.",
                    normalized_query,
                    self.max_results,
                )
                response = client.get(f"{self.base_url}/search", params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "YouTube API search failed with status %s for query '%s': %s",
                    exc.response.status_code,
                    query,
                    exc,
                )
                raise UpstreamServiceError("YouTube API search failed.") from exc
            except httpx.HTTPError as exc:
                logger.error("YouTube API connection error for query '%s': %s", query, exc)
                raise UpstreamServiceError("YouTube API is unavailable right now.") from exc

        logger.info(
            "YouTube search API response details for query='%s': %s",
            query,
            _http_response_debug_context(response),
        )
        payload = response.json()
        items = payload.get("items", [])
        if not isinstance(items, list) or not items:
            logger.error("YouTube API search returned no items for query '%s': %s", query, payload)
            raise UpstreamServiceError("No YouTube audiobook video found for this request.")

        for item in items:
            if not isinstance(item, dict):
                continue
            video_id = str(((item.get("id") or {}).get("videoId")) or "").strip()
            if not video_id:
                continue
            snippet = item.get("snippet") or {}
            title = str(snippet.get("title") or query).strip() or query
            author = str(snippet.get("channelTitle") or "Unknown channel").strip()
            summary = (
                str(snippet.get("description") or f"YouTube audiobook source for {query}.").strip()
                or f"YouTube audiobook source for {query}."
            )
            return {
                "title": title,
                "author": author or "Unknown channel",
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
                "summary": summary,
            }

        logger.error(
            "YouTube API search returned items but without usable videoId for query '%s': %s",
            query,
            payload,
        )
        raise UpstreamServiceError("No usable YouTube audiobook video found for this request.")


class NarrativeClientFactory:
    @staticmethod
    def create(
    ) -> WebResearchNarrativeClient | OpenAiSearchNarrativeClient | YouTubeSearchNarrativeClient:
        provider = (
            os.getenv("NARRATIVE_MODE_PROVIDER", NarrativeModeProvider.WEB_RESEARCH_TTS)
            .strip()
            .upper()
        )
        if provider in {
            NarrativeModeProvider.WEB_RESEARCH_TTS,
            NarrativeModeProvider.THIRDY_PARTS_STORYTEL,
        }:
            return WebResearchNarrativeClient()
        if provider == NarrativeModeProvider.OPENAI_SEARCH:
            return OpenAiSearchNarrativeClient()
        if provider == NarrativeModeProvider.YOUTUBE_SEARCH:
            return YouTubeSearchNarrativeClient()
        logger.error("Unsupported NARRATIVE_MODE_PROVIDER configured: %s", provider)
        raise UpstreamServiceError("Unsupported NARRATIVE_MODE_PROVIDER configuration.")


class YouTubeStoryPromptClient:
    def __init__(self) -> None:
        self.timeout = float(os.getenv("YOUTUBE_API_TIMEOUT_SECONDS", "20"))
        self.story_prompt_model = os.getenv("OPENAI_STORY_PROMPT_MODEL", "gpt-4.1-mini")
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.ytt_api = YouTubeTranscriptApi()

    def build_story_prompt(
        self,
        *,
        video_id: str,
        current_seconds: float,
        delta_seconds: float,
        fallback_text: str,
    ) -> tuple[str, str]:
        transcript_text = self._extract_transcript_window(
            video_id=video_id,
            current_seconds=current_seconds,
            delta_seconds=delta_seconds,
        )
        source_text = transcript_text or fallback_text.strip()
        if not source_text:
            logger.error(
                "Story prompt update failed because no source text was available. "
                "video_id=%s current_seconds=%s delta_seconds=%s",
                video_id,
                current_seconds,
                delta_seconds,
            )
            raise UpstreamServiceError("No story text is available to update the prompt.")
        return self._rewrite_prompt_with_openai(source_text), source_text

    def _extract_transcript_window(
        self,
        *,
        video_id: str,
        current_seconds: float,
        delta_seconds: float,
    ) -> str:
        language_code = self._infer_transcript_language(video_id)
        if not language_code:
            return ""
        try:
            transcript = self.ytt_api.fetch(video_id, languages=[language_code])
        except YouTubeTranscriptApiException as exc:
            logger.error(
                "YouTube transcript fetch failed for video_id=%s language=%s: %s",
                video_id,
                language_code,
                exc,
            )
            return ""

        min_seconds = max(current_seconds - delta_seconds, 0)
        max_seconds = current_seconds + delta_seconds
        snippets: list[str] = []
        for snippet in transcript:
            if isinstance(snippet, dict):
                start = float(snippet.get("start", 0) or 0)
                duration = float(snippet.get("duration", 0) or 0)
                raw_text = str(snippet.get("text", "") or "")
            else:
                start = float(getattr(snippet, "start", 0) or 0)
                duration = float(getattr(snippet, "duration", 0) or 0)
                raw_text = str(getattr(snippet, "text", "") or "")
            end = start + duration
            if end < min_seconds or start > max_seconds:
                continue
            line_text = unescape(raw_text).strip()
            line_text = re.sub(r"\s+", " ", line_text)
            if line_text:
                snippets.append(line_text)

        return " ".join(snippets).strip()

    def _infer_transcript_language(self, video_id: str) -> str:
        try:
            transcript_list = list(self.ytt_api.list(video_id))
        except YouTubeTranscriptApiException as exc:
            logger.error(
                "YouTube transcript language inference failed for video_id=%s: %s",
                video_id,
                exc,
            )
            return ""

        if not transcript_list:
            logger.error(
                "YouTube transcript language inference returned no transcript candidates "
                "for video_id=%s.",
                video_id,
            )
            return ""

        preferred_transcript = sorted(
            transcript_list,
            key=lambda transcript_item: (
                bool(getattr(transcript_item, "is_generated", True)),
                getattr(transcript_item, "language_code", "") != "en",
            ),
        )[0]
        language_code = (
            str(getattr(preferred_transcript, "language_code", "") or "").strip().lower()
        )
        if not language_code:
            logger.error(
                "YouTube transcript language inference produced an empty language code "
                "for video_id=%s.",
                video_id,
            )
            return ""
        return language_code

    def _rewrite_prompt_with_openai(self, source_text: str) -> str:
        compact_source = source_text.strip()
        if not self.openai_api_key:
            return compact_source[:700]

        payload = {
            "model": self.story_prompt_model,
            "input": (
                "Rewrite the following audiobook moment into one concise cinematic visual prompt "
                "for a live diffusion stream. Keep essential narrative continuity and tone. "
                "Return plain text only, max 40 words.\n\n"
                f"Story moment:\n{compact_source[:3000]}"
            ),
        }
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            try:
                logger.info(
                    "Calling OpenAI story prompt rewrite model='%s' source_chars=%s.",
                    self.story_prompt_model,
                    len(compact_source),
                )
                response = client.post(
                    f"{self.openai_base_url}/responses",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "OpenAI story prompt rewrite failed with status %s: %s",
                    exc.response.status_code,
                    exc,
                )
                return compact_source[:700]
            except httpx.HTTPError as exc:
                logger.error("OpenAI story prompt rewrite connection error: %s", exc)
                return compact_source[:700]

        logger.info(
            "OpenAI story prompt rewrite response details: %s",
            _http_response_debug_context(response),
        )
        rewritten = self._parse_openai_output_text(response.json())
        return rewritten or compact_source[:700]

    def _parse_openai_output_text(self, payload: dict[str, object]) -> str:
        fragments: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text_value = str(content.get("text") or "").strip()
                    if text_value:
                        fragments.append(text_value)
        return " ".join(fragments).strip()


class DaydreamClient:
    def __init__(self) -> None:
        self.base_url = self._normalize_base_url(
            os.getenv("DAYDREAM_BASE_URL", "https://api.daydream.live")
        )
        self.api_key = os.getenv("DAYDREAM_API_KEY", "")
        self.timeout = float(os.getenv("DAYDREAM_TIMEOUT_SECONDS", "30"))

    def _normalize_base_url(self, base_url: str) -> str:
        normalized_base_url = base_url.rstrip("/")
        if normalized_base_url == "https://app.daydream.live":
            logger.error(
                "DAYDREAM_BASE_URL is set to legacy host %s; using "
                "https://api.daydream.live instead.",
                normalized_base_url,
            )
            return "https://api.daydream.live"
        return normalized_base_url

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _start_payload(self, prompt: str) -> dict[str, object]:
        return {
            "pipeline": os.getenv("DAYDREAM_PIPELINE", "streamdiffusion"),
            "params": {
                "model_id": os.getenv("DAYDREAM_MODEL_ID", "stabilityai/sd-turbo"),
                "prompt": prompt,
            },
            "name": os.getenv("DAYDREAM_STREAM_NAME", "LamiaLux stream"),
            "input_type": os.getenv("DAYDREAM_INPUT_TYPE", "whip").strip().lower(),
        }

    def _start_endpoint(self) -> str:
        endpoint = os.getenv("DAYDREAM_CANVAS_STREAM_PATH", "/v1/streams")
        if endpoint == "/api/canvas/streams":
            logger.error(
                "DAYDREAM_CANVAS_STREAM_PATH is set to legacy path %s; using /v1/streams instead.",
                endpoint,
            )
            return "/v1/streams"
        return endpoint

    def _extract_stream_session(self, data: dict[str, object]) -> StreamSession:
        session_id = str(data.get("id") or data.get("sessionId") or "")
        whip_url = str(data.get("whip_url") or data.get("whipUrl") or "")
        whep_url = str(data.get("whep_url") or data.get("whepUrl") or "")
        output_video_url = str(
            data.get("output_stream_url") or data.get("outputVideoUrl") or whep_url
        )
        if not session_id or not whip_url:
            logger.error("Daydream response missing session_id or whip_url: %s", data)
            raise UpstreamServiceError("Daydream returned incomplete WHIP session data.")
        return StreamSession(
            session_id=session_id,
            upstream_stream_id=session_id,
            whip_url=whip_url,
            whep_url=whep_url,
            initial_whep_url=whep_url,
            whep_resource_url="",
            output_video_url=output_video_url,
        )

    def create_livepeer_stream_session(self, prompt: str) -> StreamSession:
        endpoint = self._start_endpoint()
        payload = self._start_payload(prompt=prompt)

        with httpx.Client(timeout=self.timeout) as client:
            try:
                logger.info(
                    "Calling Daydream stream start endpoint='%s' base_url='%s' input_type='%s'.",
                    endpoint,
                    self.base_url,
                    payload.get("input_type"),
                )
                response = client.post(
                    f"{self.base_url}{endpoint}",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Daydream stream session start failed with status %s at %s, "
                    "payload=%s, error=%s",
                    exc.response.status_code,
                    endpoint,
                    payload,
                    exc,
                )
                raise UpstreamServiceError("Unable to create the Livepeer stream session.") from exc
            except httpx.HTTPError as exc:
                logger.error("Daydream stream session connection error at %s: %s", endpoint, exc)
                raise UpstreamServiceError("Daydream is unavailable right now.") from exc

        logger.info(
            "Daydream stream start response details: %s",
            _http_response_debug_context(response),
        )
        return self._extract_stream_session(response.json())


class CoquiTtsClient:
    def __init__(self) -> None:
        self.model_name = os.getenv("COQUI_MODEL_NAME", "tts_models/en/ljspeech/tacotron2-DDC")
        self.cache_dir = Path(os.getenv("COQUI_CACHE_DIR", "storage/coqui_tts"))

    def synthesize(self, *, session_id: str, chunk_index: int, text: str) -> CoquiTtsResult:
        normalized_text = text.strip()
        if not normalized_text:
            logger.error(
                "Coqui TTS requested without text. session_id=%s chunk_index=%s",
                session_id,
                chunk_index,
            )
            raise UpstreamServiceError("No text is available for Coqui TTS.")

        cache_key = hashlib.sha256(
            f"{self.model_name}|{session_id}|{chunk_index}|{normalized_text}".encode("utf-8")
        ).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.cache_dir / f"{cache_key}.wav"
        if output_path.exists():
            logger.info(
                "Using cached Coqui TTS audio for session_id=%s chunk_index=%s path=%s.",
                session_id,
                chunk_index,
                output_path,
            )
            return CoquiTtsResult(
                audio_path=str(output_path), mime_type="audio/wav", cache_hit=True
            )

        try:
            from TTS.api import TTS
        except ImportError as exc:
            logger.error("Coqui TTS dependency is not installed: %s", exc)
            raise UpstreamServiceError("Coqui TTS is not installed on the server.") from exc

        try:
            tts = TTS(model_name=self.model_name, progress_bar=False, gpu=False)
            with NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                tmp_path = Path(tmp_file.name)
            try:
                tts.tts_to_file(text=normalized_text, file_path=str(tmp_path))
                output_path.write_bytes(tmp_path.read_bytes())
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.error(
                "Coqui TTS synthesis failed for session_id=%s chunk_index=%s model=%s: %s",
                session_id,
                chunk_index,
                self.model_name,
                exc,
                exc_info=True,
            )
            raise UpstreamServiceError(
                "Coqui TTS could not generate audio for this chunk."
            ) from exc

        return CoquiTtsResult(audio_path=str(output_path), mime_type="audio/wav", cache_hit=False)


class PromptStreamUpdater:
    def __init__(self) -> None:
        self.base_url = os.getenv("DAYDREAM_BASE_URL", "https://api.daydream.live").rstrip("/")
        self.api_key = os.getenv("DAYDREAM_API_KEY", "")
        self.timeout = float(os.getenv("DAYDREAM_TIMEOUT_SECONDS", "30"))

    def update_prompt(self, *, upstream_stream_id: str, prompt: str) -> None:
        normalized_prompt = prompt.strip()
        if not upstream_stream_id or not normalized_prompt:
            logger.error(
                (
                    "Prompt stream update requested with missing data. "
                    "upstream_stream_id=%s prompt_length=%s"
                ),
                upstream_stream_id,
                len(normalized_prompt),
            )
            raise UpstreamServiceError("Prompt stream update requires a stream id and prompt.")

        payload = {"params": {"prompt": normalized_prompt}}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        with httpx.Client(timeout=self.timeout) as client:
            try:
                logger.info(
                    "Calling Daydream prompt update for stream_id=%s prompt_chars=%s.",
                    upstream_stream_id,
                    len(normalized_prompt),
                )
                response = client.patch(
                    f"{self.base_url}/v1/streams/{upstream_stream_id}",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Prompt stream update failed with status %s for stream %s and prompt '%s': %s",
                    exc.response.status_code,
                    upstream_stream_id,
                    normalized_prompt[:200],
                    exc,
                )
                raise UpstreamServiceError("Unable to update the stream prompt.") from exc
            except httpx.HTTPError as exc:
                logger.error(
                    "Prompt stream update connection error for stream %s: %s",
                    upstream_stream_id,
                    exc,
                )
                raise UpstreamServiceError("Prompt stream update is unavailable.") from exc

        logger.info(
            "Daydream prompt update response details: %s",
            _http_response_debug_context(response),
        )
