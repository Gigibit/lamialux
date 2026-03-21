from django.urls import path

from .views import (
    coqui_tts,
    home,
    stream_match,
    stream_prompt,
    stream_session,
    whep_proxy,
    whep_resource_proxy,
    whip_proxy,
)

urlpatterns = [
    path("", home, name="home"),
    path("streams/match", stream_match, name="stream-match"),
    path("streams/<str:session_id>", stream_session, name="stream-session"),
    path("streams/<str:session_id>/prompt", stream_prompt, name="stream-prompt"),
    path("tts/coqui", coqui_tts, name="coqui-tts"),
    path("streams/<str:session_id>/whip", whip_proxy, name="stream-whip"),
    path("streams/<str:session_id>/whep", whep_proxy, name="stream-whep"),
    path(
        "streams/<str:session_id>/whep/resource",
        whep_resource_proxy,
        name="stream-whep-resource",
    ),
]
