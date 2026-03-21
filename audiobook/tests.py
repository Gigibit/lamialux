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
    def test_start_stream_success(self, build_experience, start_stream) -> None:
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
        start_stream.return_value = StreamSession(
            session_id="abc",
            whip_url="https://upstream.example/whip",
            whep_url="https://upstream.example/whep",
            output_video_url="https://upstream.example/whep",
        )

        response = self.client.post("/", {"book_query": "Dune", "daydream_prompt": "desert storm"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PDF found, downloaded, chunked")
        self.assertContains(response, "Open downloaded PDF source")
        self.assertContains(response, "Fear is the mind killer")
        self.assertContains(response, '"sessionId": "abc"')
        self.assertContains(response, '"whipUrl": "http://testserver/streams/abc/whip"')
        self.assertContains(response, '"whepUrl": "http://testserver/streams/abc/whep"')

    @patch(
        "audiobook.views.WebResearchNarrativeClient.build_experience",
        side_effect=UpstreamServiceError("boom"),
    )
    def test_start_stream_error(self, _mock_find) -> None:
        response = self.client.post("/", {"book_query": "Dune", "daydream_prompt": "desert storm"})
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
            [(
                "dune",
                "http://example.com/dune.pdf",
                "Chapter 1",
                1,
                "Fear is the mind killer.",
            )],
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
                return {"id": "stream-123", "whip_url": "https://video.example/whip", "whep_url": "https://video.example/stream"}

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
