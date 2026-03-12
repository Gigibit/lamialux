from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger("audiobook")


class UpstreamServiceError(Exception):
    """Raised when an upstream provider returns an error."""


@dataclass
class BookResult:
    title: str
    author: str
    stream_url: str
    cover_url: str


class StorytelClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("STORYTEL_BASE_URL", "https://www.storytel.com")
        self.api_key = os.getenv("STORYTEL_API_KEY", "")
        self.market = os.getenv("STORYTEL_MARKET", "us")
        self.timeout = float(os.getenv("STORYTEL_TIMEOUT_SECONDS", "15"))

    def find_audiobook(self, query: str) -> BookResult:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        endpoint = os.getenv("STORYTEL_SEARCH_PATH", "/api/search")
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.get(
                    f"{self.base_url}{endpoint}",
                    params={"query": query, "market": self.market},
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Storytel search failed with status %s for query '%s': %s",
                    exc.response.status_code,
                    query,
                    exc,
                )
                raise UpstreamServiceError("Storytel search request failed.") from exc
            except httpx.HTTPError as exc:
                logger.error("Storytel search connection error for query '%s': %s", query, exc)
                raise UpstreamServiceError("Storytel search unavailable.") from exc

        data = response.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            logger.error("Storytel returned no items for query '%s'.", query)
            raise UpstreamServiceError("No audiobook found for your request.")

        item = items[0]
        stream_url = item.get("audioUrl") or item.get("streamUrl")
        if not stream_url:
            logger.error("Storytel item missing stream URL for query '%s': %s", query, item)
            raise UpstreamServiceError("Selected audiobook has no stream URL.")

        return BookResult(
            title=item.get("title", "Unknown title"),
            author=item.get("author", "Unknown author"),
            stream_url=stream_url,
            cover_url=item.get("coverUrl", ""),
        )


class DaydreamClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("DAYDREAM_BASE_URL", "")
        self.api_key = os.getenv("DAYDREAM_API_KEY", "")
        self.timeout = float(os.getenv("DAYDREAM_TIMEOUT_SECONDS", "30"))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def start_canvas_stream(self, audio_stream_url: str, prompt: str) -> dict[str, str]:
        endpoint = os.getenv("DAYDREAM_CANVAS_STREAM_PATH", "/api/canvas/streams")
        if not self.base_url:
            logger.error("DAYDREAM_BASE_URL missing while starting stream.")
            raise UpstreamServiceError("Daydream configuration is incomplete.")

        payload = {
            "audioInputStream": audio_stream_url,
            "prompt": prompt,
            "engine": os.getenv("DAYDREAM_ENGINE", "stable-diffusion"),
        }

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
                    "Daydream stream start failed with status %s, payload=%s, error=%s",
                    exc.response.status_code,
                    payload,
                    exc,
                )
                raise UpstreamServiceError("Daydream failed to initialize the stream.") from exc
            except httpx.HTTPError as exc:
                logger.error(
                    "Daydream stream start connection error with payload=%s: %s",
                    payload,
                    exc,
                )
                raise UpstreamServiceError("Daydream stream service unavailable.") from exc

        data = response.json()
        session_id = data.get("sessionId", "")
        output_video_url = data.get("outputVideoUrl", "")
        if not session_id or not output_video_url:
            logger.error("Daydream response missing sessionId/outputVideoUrl: %s", data)
            raise UpstreamServiceError("Daydream returned incomplete stream data.")

        return {"session_id": session_id, "output_video_url": output_video_url}

    def update_prompt(self, session_id: str, prompt: str) -> None:
        endpoint_template = os.getenv(
            "DAYDREAM_PROMPT_UPDATE_PATH_TEMPLATE",
            "/api/canvas/streams/{session_id}/prompt",
        )
        endpoint = endpoint_template.format(session_id=session_id)

        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.patch(
                    f"{self.base_url}{endpoint}",
                    headers=self._headers(),
                    json={"prompt": prompt},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Daydream prompt update failed with status %s for session %s "
                    "and prompt '%s': %s",
                    exc.response.status_code,
                    session_id,
                    prompt,
                    exc,
                )
                raise UpstreamServiceError("Unable to update Daydream prompt.") from exc
            except httpx.HTTPError as exc:
                logger.error(
                    "Daydream prompt update connection error for session %s and prompt '%s': %s",
                    session_id,
                    prompt,
                    exc,
                )
                raise UpstreamServiceError("Daydream prompt update service unavailable.") from exc
