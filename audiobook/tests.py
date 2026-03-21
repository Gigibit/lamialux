from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from audiobook.services import NarrativeChunk, UpstreamServiceError, WebResearchNarrativeClient


class HomeViewTests(TestCase):
    def test_static_stylesheet_serves_css_content_type(self) -> None:
        response = self.client.get("/static/audiobook/style.css")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";")[0], "text/css")

    def setUp(self) -> None:
        self.client = Client()

    def test_home_renders(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    @patch("audiobook.views.DaydreamClient.start_canvas_stream")
    @patch("audiobook.views.StorytelClient.find_audiobook")
    def test_start_stream_success(self, find_audiobook, start_stream) -> None:
        find_audiobook.return_value = type(
            "Book",
            (),
            {
                "title": "Book",
                "author": "Author",
                "stream_url": "http://audio",
                "cover_url": "",
            },
        )()
        start_stream.return_value = {"session_id": "abc", "output_video_url": "http://video"}

        response = self.client.post(
            "/",
            {"action": "start", "book_query": "Dune", "daydream_prompt": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Stream initialized")
        self.assertContains(response, "http://video")

    @patch(
        "audiobook.views.StorytelClient.find_audiobook",
        side_effect=UpstreamServiceError("boom"),
    )
    def test_start_stream_error(self, _mock_find) -> None:
        response = self.client.post(
            "/",
            {"action": "start", "book_query": "Dune", "daydream_prompt": ""},
        )
        self.assertEqual(response.status_code, 502)
        self.assertContains(response, "boom", status_code=502)

    @override_settings()
    @patch.dict("os.environ", {"NARRATIVE_MODE_PROVIDER": "WEB_RESEARCH_TTS"}, clear=False)
    @patch("audiobook.views.DaydreamClient.start_canvas_stream")
    @patch("audiobook.views.WebResearchNarrativeClient.build_experience")
    def test_start_web_research_mode_success(self, build_experience, start_stream) -> None:
        build_experience.return_value = type(
            "Experience",
            (),
            {
                "title": "Dune · Chapter 1",
                "author": "Web research PDF",
                "cover_url": "",
                "audio_stream_url": "http://tts",
                "summary": "summary",
                "pdf_url": "http://example.com/dune.pdf",
                "storage_path": "storage/web.sqlite3",
                "chunks": [
                    NarrativeChunk(
                        chapter_title="Chapter 1",
                        chunk_index=1,
                        text="Fear is the mind killer.",
                        prompt="Dream a desert storm.",
                    )
                ],
            },
        )()
        start_stream.return_value = {"session_id": "abc", "output_video_url": "http://video"}

        response = self.client.post(
            "/", {"action": "start", "book_query": "Dune", "daydream_prompt": ""}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Web research narrative initialized")
        self.assertContains(response, "Open sourced PDF")
        self.assertContains(response, "Fear is the mind killer")

    def test_invalid_action_returns_400(self) -> None:
        response = self.client.post("/", {"action": "nope"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Invalid action requested", status_code=400)


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
                    prompt="Dream a desert storm.",
                )
            ],
        )

        self.assertTrue(sqlite_path.exists())


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
            self.assertEqual(
                client._search_pdf("Dune"),
                "https://example.com/dune.pdf?download=1",
            )

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


class HomeViewUnexpectedErrorTests(TestCase):
    def setUp(self) -> None:
        self.client = Client()

    @patch(
        "audiobook.views.WebResearchNarrativeClient.build_experience",
        side_effect=RuntimeError("unexpected boom"),
    )
    @patch.dict("os.environ", {"NARRATIVE_MODE_PROVIDER": "WEB_RESEARCH_TTS"}, clear=False)
    def test_unexpected_start_error_returns_500(self, _mock_build_experience) -> None:
        with self.assertLogs("audiobook", level="ERROR") as logs:
            response = self.client.post(
                "/", {"action": "start", "book_query": "Dune", "daydream_prompt": ""}
            )

        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "An unexpected server error occurred.", status_code=500)
        self.assertTrue(
            any("Unhandled error while processing action 'start'" in msg for msg in logs.output)
        )
        self.assertTrue(
            any(
                "Returning non-2xx response for unhandled server error: status=500" in msg
                for msg in logs.output
            )
        )
