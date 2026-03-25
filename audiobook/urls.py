from django.urls import path

from .views import (
    coqui_tts,
    home,
    music,
    spotify_web_playback_token,
    stream_match,
    stream_prompt,
    stream_session,
    stream_story_prompt,
    whep_proxy,
    whep_resource_proxy,
    whip_proxy,
)

urlpatterns = [
    path("", home, name="home"),
    path("music", music, name="music"),
    path(
        "spotify/web-playback/token",
        spotify_web_playback_token,
        name="spotify-web-playback-token",
    ),
    path("streams/match", stream_match, name="stream-match"),
    path("streams/<str:session_id>", stream_session, name="stream-session"),
    path("streams/<str:session_id>/prompt", stream_prompt, name="stream-prompt"),
    path("streams/<str:session_id>/story-prompt", stream_story_prompt, name="stream-story-prompt"),
    path("tts/coqui", coqui_tts, name="coqui-tts"),
    path("streams/<str:session_id>/whip", whip_proxy, name="stream-whip"),
    path("streams/<str:session_id>/whep", whep_proxy, name="stream-whep"),
    path(
        "streams/<str:session_id>/whep/resource",
        whep_resource_proxy,
        name="stream-whep-resource",
    ),
]
