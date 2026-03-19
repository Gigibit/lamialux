from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from audiobook.services import NarrativeChunk, UpstreamServiceError, WebResearchNarrativeClient


class HomeViewTests(TestCase):
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
