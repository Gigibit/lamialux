from unittest.mock import patch

from django.test import Client, TestCase

from audiobook.services import UpstreamServiceError


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
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "boom")
