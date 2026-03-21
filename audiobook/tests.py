import json
from pathlib import Path
from unittest.mock import patch

import httpx
from django.test import Client, TestCase

from audiobook.services import (
    DaydreamClient,
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

    def test_static_stylesheet_serves_css_content_type(self) -> None:
        response = self.client.get("/static/audiobook/style.css")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";")[0], "text/css")

    def test_home_renders(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="theia-canvas"')
        self.assertContains(response, 'id="theia-canvas-overlay"')
        self.assertNotContains(response, 'id="narration-canvas"')
        self.assertContains(response, "Canvas preview")
        self.assertContains(response, "Canvas ready. Search and prepare a stream")
        self.assertContains(response, 'id="connect-stream" disabled')
        self.assertContains(response, "const hasPreparedStream = false;")
        self.assertContains(response, 'id="coqui-player"')
        self.assertNotContains(response, "Open downloaded PDF source")

    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    @patch("audiobook.views.NarrativeClientFactory.create")
    def test_start_stream_success(
        self, create_narrative_client, create_livepeer_stream_session
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
        self.assertContains(response, "Narrative source found, media prepared")
        self.assertContains(response, "Session not matched yet.")
        self.assertNotContains(response, "Open downloaded PDF source")
        self.assertContains(response, "Fear is the mind killer")
        self.assertContains(response, 'id="theia-canvas"')
        self.assertContains(response, 'id="theia-canvas-overlay"')
        self.assertNotContains(response, 'id="narration-canvas"')
        self.assertContains(response, "const hasPreparedStream = true;")
        self.assertContains(response, 'name="browser_session_id" id="browser-session-id"')
        self.assertContains(
            response,
            "localStorage.setItem('lamialux.browserSessionId', browserSessionId)",
        )
        self.assertContains(response, "const refreshStreamSessionAfterWhip = async () => {")
        self.assertContains(response, "await refreshStreamSessionAfterWhip();")
        self.assertContains(
            response,
            "Missing Livepeer session metadata required for WHIP publishing.",
        )
        self.assertContains(
            response,
            "Unable to update the Theia canvas overlay because the overlay element is missing.",
        )
        self.assertContains(response, "const PROMPT_UPDATE_INTERVAL_MS = 5000;")
        self.assertContains(response, "/tts/coqui?sessionId=")
        self.assertContains(response, "startPromptUpdates();")
        self.assertContains(response, "/prompt")
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

    @patch("audiobook.views.NarrativeClientFactory.create")
    def test_start_stream_error(self, create_narrative_client) -> None:
        create_narrative_client.return_value.build_experience.side_effect = UpstreamServiceError(
            "boom"
        )
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
    def test_whip_proxy_saves_playback_url_header_as_whep_target(self, http_client) -> None:
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
