import json
from pathlib import Path
from unittest.mock import patch

import httpx
from django.test import Client, TestCase

from audiobook.services import (
    DaydreamClient,
    MusicSearchClient,
    NarrativeChunk,
    NarrativeClientFactory,
    OpenAiSearchNarrativeClient,
    StreamSession,
    UpstreamServiceError,
    WebResearchNarrativeClient,
)
from audiobook.views import STREAM_SESSIONS


class HomeViewTests(TestCase):
    def setUp(self) -> None:
        self.client = Client()
        STREAM_SESSIONS.clear()

    def test_home_renders(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-page="book"')
        self.assertContains(response, 'href="/music"')
        self.assertContains(response, 'id="whep-player" autoplay playsinline muted')
        self.assertNotContains(response, 'controls autoplay')
        self.assertContains(response, 'id="play-media" disabled')
        self.assertContains(response, 'class="page-flip-card" id="page-flip-card"')

    def test_music_page_renders(self) -> None:
        response = self.client.get("/music")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-page="music"')
        self.assertContains(response, 'Spotify search · Livepeer WHIP/WHEP')
        self.assertContains(response, 'Search track and prepare stream')
        self.assertContains(response, 'id="music-player" preload="none"')

    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    @patch("audiobook.views.WebResearchNarrativeClient.build_experience")
    def test_start_book_stream_success(
        self,
        build_experience,
        create_livepeer_stream_session,
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
                "browser_session_id": "browser-uuid",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PDF found, downloaded, chunked")
        self.assertContains(response, "Narrative source found, media prepared")
        self.assertContains(response, "Session not matched yet.")
        self.assertNotContains(response, "Open downloaded PDF source")
        self.assertContains(response, "Fear is the mind killer")
        self.assertContains(response, 'data-page="book"')
        self.assertContains(response, "window.lamialuxPageConfig")
        self.assertEqual(STREAM_SESSIONS["browser-uuid"].whip_url, "https://video.example/whip")

    @patch("audiobook.views.MusicSearchClient.search_track")
    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    def test_start_music_stream_success(self, create_livepeer_stream_session, search_track) -> None:
        search_track.return_value = type(
            "Track",
            (),
            {
                "title": "Teardrop",
                "artist": "Massive Attack",
                "album": "Mezzanine",
                "cover_image_url": "https://example.com/cover.jpg",
                "external_url": "https://open.spotify.com/track/123",
                "preview_url": "https://example.com/preview.mp3",
                "provider": "SPOTIFY",
            },
        )()
        create_livepeer_stream_session.return_value = StreamSession(
            session_id="livepeer-456",
            whip_url="https://video.example/whip",
            whep_url="https://video.example/whep",
            output_video_url="https://video.example/whep",
            upstream_stream_id="livepeer-456",
        )

        response = self.client.post(
            "/music",
            {
                "music_query": "Teardrop",
                "daydream_prompt": "blue liquid light",
                "browser_session_id": "browser-music",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Spotify track found and the visual music stream is ready")
        self.assertContains(response, "Teardrop")
        self.assertContains(response, "Massive Attack")
        self.assertContains(response, 'data-page="music"')
        self.assertEqual(STREAM_SESSIONS["browser-music"].whip_url, "https://video.example/whip")

    @patch(
        "audiobook.views.MusicSearchClient.search_track",
        side_effect=UpstreamServiceError("spotify boom"),
    )
    def test_music_start_error(self, _mock_search) -> None:
        response = self.client.post(
            "/music",
            {
                "music_query": "Teardrop",
                "daydream_prompt": "blue liquid light",
                "browser_session_id": "browser-music",
            },
        )

        self.assertEqual(response.status_code, 502)
        self.assertContains(response, "spotify boom", status_code=502)

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

    @patch("audiobook.views.PromptStreamUpdater.update_prompt")
    def test_stream_prompt_updates_existing_stream(self, update_prompt) -> None:
        STREAM_SESSIONS["browser-uuid"] = StreamSession(
            session_id="browser-uuid",
            whip_url="https://video.example/whip",
            whep_url="https://video.example/whep",
            output_video_url="https://video.example/whep",
            upstream_stream_id="livepeer-123",
        )

        response = self.client.post(
            "/streams/browser-uuid/prompt",
            data=json.dumps({"prompt": "Random sentence from the book."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        update_prompt.assert_called_once_with(
            upstream_stream_id="livepeer-123",
            prompt="Random sentence from the book.",
        )

    @patch("audiobook.views.CoquiTtsClient.synthesize")
    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    @patch("audiobook.views.NarrativeClientFactory.create")
    def test_coqui_tts_serves_generated_audio(
        self,
        create_narrative_client,
        create_livepeer_stream_session,
        synthesize,
    ) -> None:
        create_narrative_client.return_value.build_experience.return_value = type(
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

        audio_path = Path("storage/test-coqui.wav")
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"RIFF0000WAVEfmt ")
        synthesize.return_value = type(
            "CoquiResult",
            (),
            {"audio_path": str(audio_path), "mime_type": "audio/wav"},
        )()

        audio_response = self.client.get(
            "/tts/coqui", {"sessionId": "browser-uuid", "chunkIndex": "1"}
        )

        self.assertEqual(audio_response.status_code, 200)
        self.assertEqual(audio_response["Content-Type"], "audio/wav")
        synthesize.assert_called_once()
        audio_path.unlink(missing_ok=True)

    @patch("audiobook.views.NarrativeClientFactory.create")
    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    def test_source_media_serves_prepared_video_audio(
        self,
        create_livepeer_stream_session,
        create_narrative_client,
    ) -> None:
        media_path = Path("storage/test-source.mp4")
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"0000")
        create_narrative_client.return_value.build_experience.return_value = type(
            "Experience",
            (),
            {
                "title": "Dune Audiobook",
                "author": "Frank Herbert",
                "pdf_url": "http://example.com/dune.mp4",
                "storage_path": "storage/openai_search_media",
                "source_audio_path": str(media_path),
                "source_audio_mime_type": "video/mp4",
                "chunks": [
                    NarrativeChunk(
                        chapter_title="Audiobook source",
                        chunk_index=1,
                        text="Streaming audio for Dune.",
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

        audio_response = self.client.get(
            "/tts/coqui", {"sessionId": "browser-uuid", "chunkIndex": "1"}
        )

        self.assertEqual(audio_response.status_code, 200)
        self.assertEqual(audio_response["Content-Type"], "video/mp4")
        media_path.unlink(missing_ok=True)


class WebResearchNarrativeClientTests(TestCase):
    def test_chunk_text_splits_large_text(self) -> None:
        client = WebResearchNarrativeClient()
        client.chunk_size = 20

        chunks = client._chunk_text("one two three four five six seven eight")

        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(chunk for chunk in chunks))

    def test_normalize_pdf_url_supports_duckduckgo_redirect(self) -> None:
        client = WebResearchNarrativeClient()
        url = client._normalize_pdf_url(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fbook.pdf"
        )
        self.assertEqual(url, "https://example.com/book.pdf")


class MusicSearchClientTests(TestCase):
    def test_search_track_requires_supported_provider(self) -> None:
        client = MusicSearchClient()
        client.provider = "APPLE"

        with self.assertRaises(UpstreamServiceError):
            client.search_track("Teardrop")


class DaydreamClientTests(TestCase):
    def test_extract_stream_session_requires_whip_url(self) -> None:
        client = DaydreamClient()
        with self.assertRaises(UpstreamServiceError):
            client._extract_stream_session({"id": "abc"})


class NarrativeClientFactoryTests(TestCase):
    @patch.dict("os.environ", {"NARRATIVE_MODE_PROVIDER": "OPENAI_SEARCH"}, clear=False)
    def test_factory_returns_openai_search_client(self) -> None:
        self.assertIsInstance(NarrativeClientFactory.create(), OpenAiSearchNarrativeClient)


class OpenAiSearchNarrativeClientTests(TestCase):
    def test_parse_openai_search_response_extracts_json(self) -> None:
        client = OpenAiSearchNarrativeClient()

        parsed = client._parse_openai_search_response(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"title":"Dune Audiobook","author":"Frank Herbert",'
                                    '"video_url":"https://example.com/dune.mp4",'
                                    '"summary":"Streaming audio for Dune."}'
                                ),
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(parsed["video_url"], "https://example.com/dune.mp4")
