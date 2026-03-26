import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from django.core.files.uploadedfile import SimpleUploadedFile
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
    YouTubeSearchNarrativeClient,
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
        self.assertNotContains(response, 'id="play-media"')
        self.assertContains(response, 'class="page-flip-card" id="page-flip-card"')
        self.assertContains(response, "narratorEnabled: false")

    def test_home_sets_narrator_enabled_from_env(self) -> None:
        previous_value = os.environ.get("NARRATOR_ENABLED")
        os.environ["NARRATOR_ENABLED"] = "true"
        try:
            response = self.client.get("/")
        finally:
            if previous_value is None:
                os.environ.pop("NARRATOR_ENABLED", None)
            else:
                os.environ["NARRATOR_ENABLED"] = previous_value

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "narratorEnabled: true")

    def test_music_page_renders(self) -> None:
        response = self.client.get("/music")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-page="music"')
        self.assertContains(response, 'Spotify search · Livepeer WHIP/WHEP')
        self.assertContains(response, 'Search track and prepare stream')
        self.assertContains(response, 'id="music-player" preload="none"')
        self.assertContains(response, 'id="theia-canvas"')
        self.assertNotContains(response, 'id="narration-canvas"')
        self.assertContains(response, "Visual music preview")
        self.assertContains(response, 'id="fullscreen-output"')
        self.assertContains(response, 'id="coqui-player"')
        self.assertContains(
            response,
            'id="movie-source-player" preload="metadata" muted playsinline hidden',
        )
        self.assertNotContains(response, "Open downloaded PDF source")

    def test_movie_page_renders(self) -> None:
        response = self.client.get("/movie")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-page="movie"')
        self.assertContains(response, 'Movie upload · Livepeer WHIP/WHEP')
        self.assertContains(response, 'Upload movie and prepare stream')
        self.assertContains(
            response,
            'id="movie-source-player" preload="metadata" muted playsinline',
        )
        self.assertNotContains(response, 'id="theia-canvas"')

    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    @patch("audiobook.views.NarrativeClientFactory.create")
    def test_start_book_stream_success(
        self,
        create_client,
        create_livepeer_stream_session,
    ) -> None:
        create_client.return_value.build_experience.return_value = type(
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
                "browser_session_id": "browser-uuid",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Session not matched yet.")
        self.assertNotContains(response, "Open downloaded PDF source")
        self.assertContains(response, "Dune · Chapter 1")
        self.assertContains(response, 'data-page="book"')
        self.assertContains(response, "window.lamialuxPageConfig")
        self.assertContains(response, "Search, download, and read")
        self.assertEqual(STREAM_SESSIONS["browser-uuid"].whip_url, "https://video.example/whip")
        create_livepeer_stream_session.assert_called_once_with(
            "Mouth. Real Representation. Dune · Chapter 1. REAL, NOT drawn, NOT blurry, "
            "NOT low quality, NOT flat, NOT 2d"
        )
    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    @patch("audiobook.views.NarrativeClientFactory.create")
    def test_youtube_mode_hides_narrative_source_video_src(
        self,
        create_client,
        create_livepeer_stream_session,
    ) -> None:
        create_client.return_value.build_experience.return_value = type(
            "Experience",
            (),
            {
                "title": "Video source",
                "author": "YouTube",
                "pdf_url": "https://www.youtube.com/watch?v=DfK0b66vq8E",
                "storage_path": "youtube-search-api",
                "chunks": [
                    NarrativeChunk(
                        chapter_title="Audiobook source",
                        chunk_index=1,
                        text="Video summary.",
                    )
                ],
                "source_video_url": "https://www.youtube.com/watch?v=DfK0b66vq8E",
            },
        )()
        create_livepeer_stream_session.return_value = StreamSession(
            session_id="livepeer-youtube",
            whip_url="https://video.example/whip",
            whep_url="https://video.example/whep",
            output_video_url="https://video.example/whep",
            upstream_stream_id="livepeer-youtube",
        )

        with patch.dict("os.environ", {"NARRATIVE_MODE_PROVIDER": "YOUTUBE_SEARCH"}):
            response = self.client.post(
                "/",
                {
                    "book_query": "Dune",
                    "browser_session_id": "browser-youtube",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sourceVideoUrl:")
        self.assertContains(response, "DfK0b66vq8E")

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
                "spotify_uri": "spotify:track:123",
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
        self.assertContains(response, "Connect WHIP/WHEP to publish the canvas")
        self.assertContains(response, "Teardrop")
        self.assertContains(response, "Massive Attack")
        self.assertContains(response, 'data-page="music"')
        self.assertEqual(STREAM_SESSIONS["browser-music"].whip_url, "https://video.example/whip")

    @patch("audiobook.views.DaydreamClient.create_livepeer_stream_session")
    def test_start_movie_stream_success(self, create_livepeer_stream_session) -> None:
        create_livepeer_stream_session.return_value = StreamSession(
            session_id="livepeer-movie",
            whip_url="https://video.example/whip",
            whep_url="https://video.example/whep",
            output_video_url="https://video.example/whep",
            upstream_stream_id="livepeer-movie",
        )

        response = self.client.post(
            "/movie",
            {
                "daydream_prompt": "cinematic dreamy lighting",
                "browser_session_id": "browser-movie",
                "movie_file": SimpleUploadedFile(
                    "clip.mp4",
                    b"\x00\x00\x00\x18ftypmp42",
                    content_type="video/mp4",
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Uploaded movie")
        self.assertContains(response, 'data-page="movie"')
        self.assertEqual(STREAM_SESSIONS["browser-movie"].whip_url, "https://video.example/whip")

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


    @patch.dict(
        "os.environ",
        {
            "SPOTIFY_WEB_PLAYBACK_ACCESS_TOKEN": "token-123",
            "SPOTIFY_WEB_PLAYBACK_ACCESS_TOKEN_SCOPES": (
                "streaming user-read-email user-read-private user-modify-playback-state"
            ),
        },
    )
    def test_spotify_web_playback_token_endpoint_success(self) -> None:
        response = self.client.get("/spotify/web-playback/token")

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content.decode("utf-8"), {"access_token": "token-123"})

    def test_spotify_web_playback_token_endpoint_missing_token(self) -> None:
        response = self.client.get("/spotify/web-playback/token")

        self.assertEqual(response.status_code, 503)
        self.assertContains(
            response,
            "Spotify Web Playback token is not configured",
            status_code=503,
        )

    @patch.dict(
        "os.environ",
        {
            "SPOTIFY_CLIENT_ID": "spotify-client-id",
            "SPOTIFY_REDIRECT_URI": "https://example.com/music",
        },
    )
    @patch("audiobook.views.httpx.Client")
    def test_spotify_web_playback_token_endpoint_exchanges_authorization_code(
        self, http_client
    ) -> None:
        class DummyResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "access_token": "fresh-access-token",
                    "refresh_token": "fresh-refresh-token",
                    "expires_in": 3600,
                    "scope": (
                        "streaming user-read-email user-read-private "
                        "user-modify-playback-state"
                    ),
                }

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def post(self, url, *, data, auth, headers):
                self.url = url
                self.data = data
                self.auth = auth
                self.headers = headers
                return DummyResponse()

        dummy_client = DummyClient()
        http_client.return_value = dummy_client

        session = self.client.session
        session["spotify_oauth_state"] = "state-123"
        session["spotify_oauth_code_verifier"] = "verifier-123"
        session.save()

        response = self.client.get("/spotify/web-playback/token?code=auth-code-123&state=state-123")

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content.decode("utf-8"), {"access_token": "fresh-access-token"}
        )
        self.assertEqual(dummy_client.url, "https://accounts.spotify.com/api/token")
        self.assertEqual(
            dummy_client.data,
            {
                "grant_type": "authorization_code",
                "code": "auth-code-123",
                "redirect_uri": "https://example.com/music",
                "code_verifier": "verifier-123",
                "client_id": "spotify-client-id",
            },
        )
        self.assertIsNone(dummy_client.auth)
        self.assertEqual(
            self.client.session["spotify_web_playback_refresh_token"], "fresh-refresh-token"
        )

    @patch.dict(
        "os.environ",
        {
            "SPOTIFY_CLIENT_ID": "spotify-client-id",
            "SPOTIFY_CLIENT_SECRET": "spotify-client-secret",
        },
    )
    @patch("audiobook.views.httpx.Client")
    def test_spotify_web_playback_token_endpoint_uses_refresh_token_when_available(
        self, http_client
    ) -> None:
        session = self.client.session
        session["spotify_web_playback_refresh_token"] = "saved-refresh-token"
        session.save()

        class DummyResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "access_token": "refresh-access-token",
                    "expires_in": 3600,
                    "scope": (
                        "streaming user-read-email user-read-private "
                        "user-modify-playback-state"
                    ),
                }

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def post(self, url, *, data, auth, headers):
                self.data = data
                return DummyResponse()

        dummy_client = DummyClient()
        http_client.return_value = dummy_client

        response = self.client.get("/spotify/web-playback/token")

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content.decode("utf-8"), {"access_token": "refresh-access-token"}
        )
        self.assertEqual(
            dummy_client.data,
            {
                "grant_type": "refresh_token",
                "refresh_token": "saved-refresh-token",
                "client_id": "spotify-client-id",
            },
        )

    @patch.dict(
        "os.environ",
        {
            "SPOTIFY_CLIENT_ID": "spotify-client-id",
            "SPOTIFY_REDIRECT_URI": "https://example.com/music",
        },
        clear=True,
    )
    def test_spotify_web_playback_token_endpoint_returns_authorization_url(self) -> None:
        response = self.client.get("/spotify/web-playback/token")

        self.assertEqual(response.status_code, 401)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["error"], "Spotify authorization is required.")
        self.assertIn("authorization_url", payload)

    @patch.dict(
        "os.environ",
        {
            "SPOTIFY_CLIENT_ID": "spotify-client-id",
            "SPOTIFY_REDIRECT_URI": "https://example.com/music",
        },
    )
    @patch("audiobook.views.httpx.Client")
    def test_spotify_web_playback_token_endpoint_rejects_missing_scopes(
        self, http_client
    ) -> None:
        class DummyResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "access_token": "fresh-access-token",
                    "refresh_token": "fresh-refresh-token",
                    "expires_in": 3600,
                    "scope": "user-read-email user-read-private",
                }

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def post(self, url, *, data, auth, headers):
                return DummyResponse()

        http_client.return_value = DummyClient()
        session = self.client.session
        session["spotify_oauth_state"] = "state-123"
        session["spotify_oauth_code_verifier"] = "verifier-123"
        session.save()

        response = self.client.get("/spotify/web-playback/token?code=auth-code-123&state=state-123")

        self.assertEqual(response.status_code, 401)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(
            payload["error"], "Spotify authorization is required with Web Playback scopes."
        )
        self.assertEqual(
            payload["missing_scopes"], ["streaming", "user-modify-playback-state"]
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

    @patch.dict("os.environ", {"NARRATIVE_MODE_PROVIDER": "YOUTUBE_SEARCH"}, clear=False)
    def test_factory_returns_youtube_search_client(self) -> None:
        self.assertIsInstance(NarrativeClientFactory.create(), YouTubeSearchNarrativeClient)


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


class YouTubeSearchNarrativeClientTests(TestCase):
    @patch.dict("os.environ", {"YOUTUBE_API_KEY": "abc123"}, clear=False)
    @patch("audiobook.services.httpx.Client")
    def test_search_video_returns_watch_url(self, http_client) -> None:
        client = YouTubeSearchNarrativeClient()

        class DummyResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {
                    "items": [
                        {
                            "id": {"videoId": "xyz987"},
                            "snippet": {
                                "title": "Dune Audiobook",
                                "channelTitle": "Narrator Channel",
                                "description": "Part 1.",
                            },
                        }
                    ]
                }

        class DummyClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def get(self, *args, **kwargs):
                return DummyResponse()

        http_client.return_value = DummyClient()
        result = client._search_video("Dune")
        self.assertEqual(result["video_url"], "https://www.youtube.com/watch?v=xyz987")


class StoryPromptViewTests(TestCase):
    def setUp(self) -> None:
        self.client = Client()
        STREAM_SESSIONS.clear()
        STREAM_SESSIONS["browser-story"] = StreamSession(
            session_id="browser-story",
            whip_url="https://video.example/whip",
            whep_url="https://video.example/whep",
            output_video_url="https://video.example/whep",
            upstream_stream_id="livepeer-story",
        )

    @patch("audiobook.views.PromptStreamUpdater.update_prompt")
    @patch("audiobook.views.YouTubeStoryPromptClient.build_story_prompt")
    def test_story_prompt_updates_from_video_window(
        self,
        build_story_prompt,
        update_prompt,
    ) -> None:
        build_story_prompt.return_value = ("cinematic skyline at dusk", "spoken sentence")

        response = self.client.post(
            "/streams/browser-story/story-prompt",
            data=json.dumps(
                {
                    "videoId": "DfK0b66vq8E",
                    "currentSeconds": 42.5,
                    "fallbackText": "fallback narrative",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        update_prompt.assert_called_once_with(
            upstream_stream_id="livepeer-story",
            prompt="cinematic skyline at dusk",
        )


class YouTubeStoryPromptClientTests(TestCase):
    def test_extract_transcript_window_filters_by_time(self) -> None:
        from audiobook.services import YouTubeStoryPromptClient

        client = YouTubeStoryPromptClient()
        client._infer_transcript_language = Mock(return_value="en")
        client.ytt_api.fetch = Mock(
            return_value=[
                {"text": "intro", "start": 0, "duration": 2},
                {"text": "target scene", "start": 8, "duration": 4},
                {"text": "ending", "start": 25, "duration": 3},
            ]
        )
        transcript = client._extract_transcript_window(
            video_id="abc123",
            current_seconds=10,
            delta_seconds=5,
        )

        self.assertEqual(transcript, "target scene")

    def test_infer_transcript_language_prefers_non_generated_english(self) -> None:
        from audiobook.services import YouTubeStoryPromptClient

        client = YouTubeStoryPromptClient()
        generated_spanish = Mock(language_code="es", is_generated=True)
        generated_english = Mock(language_code="en", is_generated=True)
        manual_english = Mock(language_code="en", is_generated=False)
        client.ytt_api.list = Mock(
            return_value=[generated_spanish, generated_english, manual_english]
        )

        language_code = client._infer_transcript_language("abc123")

        self.assertEqual(language_code, "en")
