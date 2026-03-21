import json
from pathlib import Path
from unittest.mock import patch

import httpx
from django.test import Client, TestCase

from audiobook.services import (
    DaydreamClient,
    NarrativeChunk,
    StreamSession,
    UpstreamServiceError,
    WebResearchNarrativeClient,
)
from audiobook.views import STREAM_SESSIONS


class HomeViewTests(TestCase):
    def setUp(self) -> None:
        self.client = Client()
        STREAM_SESSIONS.clear()

    def test_static_stylesheet_serves_css_content_type(self) -> None:
        response = self.client.get("/static/audiobook/style.css")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";")[0], "text/css")

    def test_home_renders(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    @patch("audiobook.views.WebResearchNarrativeClient.build_experience")
    def test_start_stream_success(self, build_experience, create_livepeer_stream_session) -> None:
        build_experience.return_value = type(
            "Experience",
            (),
            {
                "title": "Dune · Chapter 1",
                "author": "PDF source",
                "pdf_url": "http://example.com/dune.pdf",
                "storage_path": "storage/web.sqlite3",
                "chunks": [
                    NarrativeChunk(
                        chapter_title="Chapter 1",
                        chunk_index=1,
                        text="Fear is the mind killer.",
                    )
                ],
            },
        )()
        create_livepeer_stream_session.return_value = StreamSession(
            session_id="livepeer-123",
            whip_url="https://video.example/whip",
            whep_url="https://video.example/whep",
            output_video_url="https://video.example/whep",
            upstream_stream_id="livepeer-123",
        )
        response = self.client.post(
            "/",
            {
                "book_query": "Dune",
                "daydream_prompt": "desert storm",
                "browser_session_id": "browser-uuid",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PDF found, downloaded, chunked")
        self.assertContains(response, "Session not matched yet.")
        self.assertContains(response, "Open downloaded PDF source")
        self.assertContains(response, "Fear is the mind killer")
        self.assertContains(response, 'id="theia-canvas"')
        self.assertContains(response, 'name="browser_session_id" id="browser-session-id"')
        self.assertContains(
            response,
            "localStorage.setItem('lamialux.browserSessionId', browserSessionId)",
        )
        self.assertContains(
            response,
            "const refreshStreamSessionAfterWhip = async () => {",
        )
        self.assertContains(
            response,
            "await refreshStreamSessionAfterWhip();",
        )
        self.assertContains(
            response,
            "Missing Livepeer session metadata required for WHIP publishing.",
        )
        self.assertNotContains(
            response,
            "Missing Livepeer session metadata required for WHIP/WHEP.",
        )
        self.assertContains(
            response,
            "https://raw.githubusercontent.com/Gigibit/ingoya/refs/heads/theia/public/caos.js",
        )
        self.assertEqual(STREAM_SESSIONS["browser-uuid"].whip_url, "https://video.example/whip")
        self.assertEqual(STREAM_SESSIONS["browser-uuid"].whep_url, "https://video.example/whep")
        self.assertEqual(
            STREAM_SESSIONS["browser-uuid"].initial_whep_url,
            "https://video.example/whep",
        )
        self.assertEqual(STREAM_SESSIONS["livepeer-123"].session_id, "livepeer-123")

    @patch(
        "audiobook.views.WebResearchNarrativeClient.build_experience",
        side_effect=UpstreamServiceError("boom"),
    )
    def test_start_stream_error(self, _mock_find) -> None:
        response = self.client.post(
            "/",
            {
                "book_query": "Dune",
                "daydream_prompt": "desert storm",
                "browser_session_id": "browser-uuid",
            },
        )
        self.assertEqual(response.status_code, 502)
        self.assertContains(response, "boom", status_code=502)

    def test_invalid_form_returns_400(self) -> None:
        response = self.client.post("/", {"book_query": "", "daydream_prompt": ""})
        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "Please provide both a book query and a prompt",
            status_code=400,
        )

    @patch("audiobook.views.httpx.Client")
    def test_whep_proxy_returns_502_when_upstream_times_out(self, http_client) -> None:
        STREAM_SESSIONS["abc"] = StreamSession(
            session_id="abc",
            whip_url="https://upstream.example/whip",
            whep_url="https://upstream.example/whep",
            output_video_url="https://upstream.example/whep",
            upstream_stream_id="upstream-abc",
        )

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def request(self, *args, **kwargs):
                raise httpx.ReadTimeout("timed out")

        http_client.return_value = DummyClient()

        response = self.client.post(
            "/streams/abc/whep",
            data="v=0",
            content_type="application/sdp",
        )

        self.assertEqual(response.status_code, 502)
        self.assertContains(response, "WHEP upstream unavailable.", status_code=502)

    @patch("audiobook.views.httpx.Client")
    def test_whip_proxy_saves_playback_url_header_as_whep_target(
        self, http_client
    ) -> None:
        STREAM_SESSIONS["abc"] = StreamSession(
            session_id="abc",
            whip_url="https://upstream.example/whip",
            whep_url="https://upstream.example/whep",
            output_video_url="https://upstream.example/original-output",
            upstream_stream_id="upstream-abc",
        )

        class DummyResponse:
            status_code = 201
            text = "v=0"
            headers = {
                "content-type": "application/sdp",
                "livepeer-playback-url": "https://fra-ai-prod-livepeer-ai-gateway-0.livepeer.com/live/video-to-video/stk_123-out/whep",
                "location": "https://upstream.example/whip/resource/123",
            }

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def post(self, *args, **kwargs):
                return DummyResponse()

        http_client.return_value = DummyClient()

        response = self.client.post(
            "/streams/abc/whip",
            data="v=0",
            content_type="application/sdp",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            STREAM_SESSIONS["abc"].whep_url,
            "https://fra-ai-prod-livepeer-ai-gateway-0.livepeer.com/live/video-to-video/stk_123-out/whep",
        )
        self.assertEqual(
            STREAM_SESSIONS["abc"].output_video_url,
            "https://upstream.example/original-output",
        )
        self.assertEqual(
            response["livepeer-playback-url"],
            "http://testserver/streams/abc/whep",
        )
        self.assertEqual(
            response["location"],
            "http://testserver/streams/abc/whep/resource",
        )


    @patch("audiobook.views.httpx.Client")
    def test_whip_proxy_returns_502_when_playback_header_is_missing(self, http_client) -> None:
        STREAM_SESSIONS["abc"] = StreamSession(
            session_id="abc",
            whip_url="https://upstream.example/whip",
            whep_url="",
            output_video_url="https://upstream.example/original-output",
            upstream_stream_id="upstream-abc",
        )

        class DummyResponse:
            status_code = 201
            text = "v=0"
            headers = {"content-type": "application/sdp"}

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def post(self, *args, **kwargs):
                return DummyResponse()

        http_client.return_value = DummyClient()

        response = self.client.post(
            "/streams/abc/whip",
            data="v=0",
            content_type="application/sdp",
        )

        self.assertEqual(response.status_code, 502)
        self.assertContains(
            response,
            "WHIP upstream did not provide a playback URL.",
            status_code=502,
        )

    @patch("audiobook.views.httpx.Client")
    def test_whep_proxy_uses_updated_playback_url_after_whip(self, http_client) -> None:
        STREAM_SESSIONS["abc"] = StreamSession(
            session_id="abc",
            whip_url="https://upstream.example/whip",
            whep_url="https://playback.example/whep/stream",
            output_video_url="https://upstream.example/original-output",
            upstream_stream_id="upstream-abc",
        )

        class DummyResponse:
            status_code = 200
            text = "v=0"
            headers = {"content-type": "application/sdp"}

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def request(self, method, url, headers, content):
                self.last_call = {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "content": content,
                }
                return DummyResponse()

        dummy_client = DummyClient()
        http_client.return_value = dummy_client

        response = self.client.post(
            "/streams/abc/whep",
            data="v=0",
            content_type="application/sdp",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(dummy_client.last_call["url"], "https://playback.example/whep/stream")

    @patch("audiobook.views.httpx.Client")
    def test_whep_proxy_returns_upstream_404_without_fallback(self, http_client) -> None:
        STREAM_SESSIONS["abc"] = StreamSession(
            session_id="abc",
            whip_url="https://upstream.example/whip",
            whep_url="https://ai.livepeer.com/live/video-to-video/stale-out/whep",
            output_video_url="https://playback.example/whep/stream",
            upstream_stream_id="upstream-abc",
            initial_whep_url="https://fra-ai-prod-livepeer-ai-gateway-0.livepeer.com/live/video-to-video/stale-out/whep",
        )

        class DummyResponse:
            status_code = 404
            text = "not found"
            headers = {"content-type": "text/plain"}

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def request(self, method, url, headers, content):
                self.last_call = {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "content": content,
                }
                return DummyResponse()

        dummy_client = DummyClient()
        http_client.return_value = dummy_client

        response = self.client.post(
            "/streams/abc/whep",
            data="v=0",
            content_type="application/sdp",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            dummy_client.last_call["url"],
            "https://ai.livepeer.com/live/video-to-video/stale-out/whep",
        )

    @patch("audiobook.views.httpx.Client")
    def test_whep_resource_proxy_uses_upstream_resource_location_when_available(
        self, http_client
    ) -> None:
        STREAM_SESSIONS["abc"] = StreamSession(
            session_id="abc",
            whip_url="https://upstream.example/whip",
            whep_url="https://playback.example/whep/stream",
            output_video_url="https://upstream.example/original-output",
            upstream_stream_id="upstream-abc",
            whep_resource_url="https://playback.example/whep/resource/42",
        )

        class DummyResponse:
            status_code = 204
            text = ""
            headers = {"content-type": "application/trickle-ice-sdpfrag"}

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def request(self, method, url, headers, content):
                self.last_call = {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "content": content,
                }
                return DummyResponse()

        dummy_client = DummyClient()
        http_client.return_value = dummy_client

        response = self.client.patch(
            "/streams/abc/whep/resource",
            data="a=candidate:1 1 UDP 1 127.0.0.1 9000 typ host",
            content_type="application/trickle-ice-sdpfrag",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(dummy_client.last_call["method"], "PATCH")
        self.assertEqual(dummy_client.last_call["url"], "https://playback.example/whep/resource/42")

    def test_stream_match_returns_aliased_existing_stream_for_browser_uuid(self) -> None:
        STREAM_SESSIONS["upstream-abc"] = StreamSession(
            session_id="upstream-abc",
            whip_url="https://upstream.example/whip",
            whep_url="https://upstream.example/whep",
            output_video_url="https://upstream.example/whep",
            upstream_stream_id="upstream-abc",
        )

        response = self.client.post(
            "/streams/match",
            data=json.dumps({"sessionId": "browser-uuid"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "sessionId": "browser-uuid",
                "whipUrl": "http://testserver/streams/browser-uuid/whip",
                "whepUrl": "http://testserver/streams/browser-uuid/whep",
                "outputVideoUrl": "https://upstream.example/whep",
            },
        )
        self.assertEqual(STREAM_SESSIONS["browser-uuid"].whep_url, "https://upstream.example/whep")
        self.assertEqual(
            STREAM_SESSIONS["browser-uuid"].initial_whep_url,
            STREAM_SESSIONS["upstream-abc"].initial_whep_url,
        )

    def test_stream_match_returns_404_without_existing_streams(self) -> None:
        response = self.client.post(
            "/streams/match",
            data=json.dumps({"sessionId": "browser-uuid"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertJSONEqual(response.content, {"error": "No stream match found."})


    @patch("audiobook.views.uuid.uuid4", return_value="generated-browser-uuid")
    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    @patch("audiobook.views.WebResearchNarrativeClient.build_experience")
    def test_start_stream_generates_fallback_browser_session_id(
        self,
        build_experience,
        create_livepeer_stream_session,
        _mock_uuid4,
    ) -> None:
        build_experience.return_value = type(
            "Experience",
            (),
            {
                "title": "Dune · Chapter 1",
                "author": "PDF source",
                "pdf_url": "http://example.com/dune.pdf",
                "storage_path": "storage/web.sqlite3",
                "chunks": [
                    NarrativeChunk(
                        chapter_title="Chapter 1",
                        chunk_index=1,
                        text="Fear is the mind killer.",
                    )
                ],
            },
        )()
        create_livepeer_stream_session.return_value = StreamSession(
            session_id="livepeer-123",
            whip_url="https://video.example/whip",
            whep_url="https://video.example/whep",
            output_video_url="https://video.example/whep",
            upstream_stream_id="livepeer-123",
        )

        response = self.client.post(
            "/",
            {
                "book_query": "Dune",
                "daydream_prompt": "desert storm",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("generated-browser-uuid", STREAM_SESSIONS)


class WebResearchNarrativeClientTests(TestCase):
    def test_chunk_text_splits_content(self) -> None:
        client = WebResearchNarrativeClient()
        client.chunk_size = 20
        chunks = client._chunk_text("one two three four five six seven")
        self.assertGreater(len(chunks), 1)

    def test_store_chunks_writes_sqlite(self) -> None:
        client = WebResearchNarrativeClient()
        sqlite_path = Path("/tmp/lamialux-test.sqlite3")
        if sqlite_path.exists():
            sqlite_path.unlink()
        client.sqlite_path = sqlite_path

        client._store_chunks(
            query="dune",
            pdf_url="http://example.com/dune.pdf",
            chunks=[
                NarrativeChunk(
                    chapter_title="Chapter 1",
                    chunk_index=1,
                    text="Fear is the mind killer.",
                )
            ],
        )

        self.assertTrue(sqlite_path.exists())

    def test_build_experience_uses_cached_chunks_without_refetching(self) -> None:
        client = WebResearchNarrativeClient()
        sqlite_path = Path("/tmp/lamialux-cache.sqlite3")
        if sqlite_path.exists():
            sqlite_path.unlink()
        client.sqlite_path = sqlite_path

        client._store_chunks(
            query="dune",
            pdf_url="http://example.com/dune.pdf",
            chunks=[
                NarrativeChunk(
                    chapter_title="Chapter 1",
                    chunk_index=1,
                    text="Fear is the mind killer.",
                )
            ],
        )

        with patch.object(client, "_search_pdf", side_effect=AssertionError("cache miss")):
            experience = client.build_experience("dune")

        self.assertTrue(experience.cache_hit)
        self.assertEqual(experience.pdf_url, "http://example.com/dune.pdf")
        self.assertEqual(experience.chunks[0].text, "Fear is the mind killer.")

    def test_store_chunks_migrates_legacy_prompt_column(self) -> None:
        import sqlite3

        client = WebResearchNarrativeClient()
        sqlite_path = Path("/tmp/lamialux-legacy.sqlite3")
        if sqlite_path.exists():
            sqlite_path.unlink()
        client.sqlite_path = sqlite_path

        with sqlite3.connect(sqlite_path) as connection:
            connection.execute(
                """
                CREATE TABLE narrative_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    pdf_url TEXT NOT NULL,
                    chapter_title TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL
                )
                """
            )
            connection.commit()

        client._store_chunks(
            query="dune",
            pdf_url="http://example.com/dune.pdf",
            chunks=[
                NarrativeChunk(
                    chapter_title="Chapter 1",
                    chunk_index=1,
                    text="Fear is the mind killer.",
                )
            ],
        )

        with sqlite3.connect(sqlite_path) as connection:
            columns = connection.execute("PRAGMA table_info(narrative_chunks)").fetchall()
            rows = connection.execute(
                "SELECT query, pdf_url, chapter_title, chunk_index, text FROM narrative_chunks"
            ).fetchall()

        self.assertNotIn("prompt", [column[1] for column in columns])
        self.assertEqual(
            rows,
            [
                (
                    "dune",
                    "http://example.com/dune.pdf",
                    "Chapter 1",
                    1,
                    "Fear is the mind killer.",
                )
            ],
        )


class WebResearchSearchTests(TestCase):
    def test_search_pdf_returns_url_without_human_repr(self) -> None:
        client = WebResearchNarrativeClient()

        class DummyResponse:
            text = '<a rel="nofollow" class="result__a" href="https://example.com/dune.pdf?download=1">PDF</a>'

            def raise_for_status(self) -> None:
                return None

        class DummyClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def get(self, *args, **kwargs):
                return DummyResponse()

        with patch("audiobook.services.httpx.Client", DummyClient):
            self.assertEqual(client._search_pdf("Dune"), "https://example.com/dune.pdf?download=1")

    def test_search_pdf_resolves_duckduckgo_redirect_to_https_pdf(self) -> None:
        client = WebResearchNarrativeClient()

        class DummyResponse:
            text = (
                '<a rel="nofollow" class="result__a" '
                'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fbooks%2Fwuthering-heights.pdf&amp;rut=abc">'
                "PDF</a>"
            )

            def raise_for_status(self) -> None:
                return None

        class DummyClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def get(self, *args, **kwargs):
                return DummyResponse()

        with patch("audiobook.services.httpx.Client", DummyClient):
            self.assertEqual(
                client._search_pdf("cime tempestose"),
                "https://example.com/books/wuthering-heights.pdf",
            )

    def test_normalize_pdf_url_rejects_non_pdf_target(self) -> None:
        client = WebResearchNarrativeClient()

        self.assertIsNone(
            client._normalize_pdf_url(
                "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle.html&amp;rut=abc"
            )
        )


class DaydreamClientTests(TestCase):
    def test_start_canvas_stream_uses_v1_endpoint_and_payload(self) -> None:
        client = DaydreamClient()

        class DummyResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, str]:
                return {
                    "id": "stream-123",
                    "whip_url": "https://video.example/whip",
                    "whep_url": "https://video.example/stream",
                }

        class DummyHttpClient:
            def __init__(self, *args, **kwargs) -> None:
                self.post_calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def post(self, url, headers, json):
                self.post_calls.append((url, headers, json))
                self.last_call = (url, headers, json)
                return DummyResponse()

        dummy_http_client = DummyHttpClient()
        with patch("audiobook.services.httpx.Client", return_value=dummy_http_client):
            stream = client.create_livepeer_stream_session(prompt="Dreamy skyline")

        self.assertEqual(stream.session_id, "stream-123")
        self.assertEqual(stream.output_video_url, "https://video.example/stream")
        self.assertEqual(dummy_http_client.last_call[0], "https://api.daydream.live/v1/streams")
        self.assertEqual(
            dummy_http_client.last_call[2],
            {
                "pipeline": "streamdiffusion",
                "params": {
                    "model_id": "stabilityai/sd-turbo",
                    "prompt": "Dreamy skyline",
                },
                "name": "LamiaLux stream",
                "input_type": "whip",
            },
        )
