from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from pypdf import PdfReader

logger = logging.getLogger("audiobook")


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


@dataclass
class StreamSession:
    session_id: str
    whip_url: str
    whep_url: str
    output_video_url: str


class WebResearchNarrativeClient:
    def __init__(self) -> None:
        self.timeout = float(os.getenv("WEB_RESEARCH_TIMEOUT_SECONDS", "20"))
        self.chunk_size = int(os.getenv("WEB_RESEARCH_CHUNK_SIZE", "650"))
        self.sqlite_path = Path(
            os.getenv("WEB_RESEARCH_SQLITE_PATH", "storage/web_research_chunks.sqlite3")
        )
        self.search_url = os.getenv("WEB_RESEARCH_SEARCH_URL", "https://duckduckgo.com/html/")

    def build_experience(self, query: str) -> NarrativeExperience:
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

        try:
            self._store_chunks(query=query, pdf_url=pdf_url, chunks=chunks)
        except sqlite3.Error as exc:
            logger.error(
                "Failed to persist narrative chunks for query '%s' and pdf %s: %s",
                query,
                pdf_url,
                exc,
                exc_info=True,
            )
            raise UpstreamServiceError("Unable to store the extracted narrative chunks.") from exc

        return NarrativeExperience(
            title=self._derive_title(query, chunks),
            author="PDF source",
            pdf_url=pdf_url,
            chunks=chunks,
            storage_path=str(self.sqlite_path),
        )

    def _search_pdf(self, query: str) -> str:
        search_query = f"{query} filetype:pdf"
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            try:
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
            self._ensure_narrative_chunks_schema(connection)
            column_names = self._narrative_chunk_column_names(connection)
            has_prompt_column = "prompt" in column_names

            connection.execute("DELETE FROM narrative_chunks WHERE query = ?", (query,))
            if has_prompt_column:
                connection.executemany(
                    """
                    INSERT INTO narrative_chunks (
                        query, prompt, pdf_url, chapter_title, chunk_index, text
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            query,
                            query,
                            pdf_url,
                            chunk.chapter_title,
                            chunk.chunk_index,
                            chunk.text,
                        )
                        for chunk in chunks
                    ],
                )
            else:
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

    def _ensure_narrative_chunks_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS narrative_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                prompt TEXT NOT NULL DEFAULT '',
                pdf_url TEXT NOT NULL,
                chapter_title TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL
            )
            """
        )
        column_names = self._narrative_chunk_column_names(connection)
        if "prompt" not in column_names:
            connection.execute(
                "ALTER TABLE narrative_chunks ADD COLUMN prompt TEXT NOT NULL DEFAULT ''"
            )

    def _narrative_chunk_column_names(self, connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute("PRAGMA table_info(narrative_chunks)").fetchall()
        return {str(row[1]) for row in rows}


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
            whip_url=whip_url,
            whep_url=whep_url,
            output_video_url=output_video_url,
        )

    def create_livepeer_stream_session(self, prompt: str) -> StreamSession:
        endpoint = self._start_endpoint()
        payload = self._start_payload(prompt=prompt)

        with httpx.Client(timeout=self.timeout) as client:
            try:
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

        return self._extract_stream_session(response.json())
