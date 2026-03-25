import json
import logging
import os
import uuid
from pathlib import Path

import httpx
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import BookRequestForm, MusicRequestForm
from .services import (
    CoquiTtsClient,
    DaydreamClient,
    MusicSearchClient,
    NarrativeClientFactory,
    NarrativeModeProvider,
    PromptStreamUpdater,
    StreamSession,
    UpstreamServiceError,
    YouTubeStoryPromptClient,
)

logger = logging.getLogger("audiobook")
STREAM_SESSIONS: dict[str, StreamSession] = {}
WHEP_PROXY_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=30.0, pool=5.0)


def _request_debug_context(request: HttpRequest) -> dict[str, object]:
    return {
        "path": request.path,
        "content_type": request.headers.get("Content-Type"),
        "content_length": request.headers.get("Content-Length"),
        "user_agent": request.headers.get("User-Agent"),
        "remote_addr": request.META.get("REMOTE_ADDR"),
        "forwarded_for": request.headers.get("X-Forwarded-For"),
    }


def _upstream_debug_context(upstream: httpx.Response) -> dict[str, object]:
    return {
        "status": upstream.status_code,
        "content_type": upstream.headers.get("content-type"),
        "location": upstream.headers.get("location"),
        "livepeer_playback_url": (
            upstream.headers.get("livepeer-playback-url")
            or upstream.headers.get("Livepeer-Playback-Url")
        ),
        "body_preview": upstream.text[:500],
    }


def _whep_upstream_url(stream: StreamSession, method: str) -> str:
    if method in {"PATCH", "DELETE"} and stream.whep_resource_url:
        return stream.whep_resource_url
    return stream.whep_url


def _page_context(page: str) -> dict[str, object]:
    narrative_mode_provider = (
        str(os.getenv("NARRATIVE_MODE_PROVIDER", NarrativeModeProvider.WEB_RESEARCH_TTS))
        .strip()
        .upper()
    )
    return {
        "page": page,
        "narrative_mode_provider": narrative_mode_provider,
        "update_story_prompt_delta_seconds": int(
            float(os.getenv("UPDATE_STORY_PROMPT_DELTA_SECONDS", "10"))
        ),
        "book_form": BookRequestForm(),
        "music_form": MusicRequestForm(),
        "message": "",
        "error": "",
        "stream": None,
        "music_track": None,
    }


def home(request: HttpRequest) -> HttpResponse:
    return _render_page(request=request, page="book")


def music(request: HttpRequest) -> HttpResponse:
    return _render_page(request=request, page="music")


def _render_page(request: HttpRequest, *, page: str) -> HttpResponse:
    context = _page_context(page)

    if request.method == "POST":
        try:
            context, status_code = _handle_start(request, context, page=page)
            if status_code >= 400:
                logger.error(
                    "Returning non-2xx response for %s start action: status=%s", page, status_code
                )
            return render(request, "audiobook/home.html", context, status=status_code)
        except Exception as exc:
            logger.error(
                "Unhandled error while processing %s POST: %s", page, exc, exc_info=True
            )
            context["error"] = "An unexpected server error occurred."
            logger.error("Returning non-2xx response for unhandled server error: status=500")
            return render(request, "audiobook/home.html", context, status=500)

    return render(request, "audiobook/home.html", context)


@require_GET
def stream_session(request: HttpRequest, session_id: str) -> JsonResponse:
    stream = STREAM_SESSIONS.get(session_id)
    if stream is None:
        logger.error("Requested unknown stream session '%s'.", session_id)
        logger.error("Returning non-2xx response for missing stream session: status=404")
        return JsonResponse({"error": "Stream session not found."}, status=404)

    return JsonResponse(
        {
            "sessionId": stream.session_id,
            "whipUrl": request.build_absolute_uri(f"/streams/{stream.session_id}/whip"),
            "whepUrl": request.build_absolute_uri(f"/streams/{stream.session_id}/whep"),
        }
    )


@csrf_exempt
@require_POST
def stream_match(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        logger.error("Invalid stream match payload: %s", exc)
        logger.error("Returning non-2xx response for invalid stream match payload: status=400")
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    requested_session_id = str(payload.get("sessionId") or "").strip()
    if not requested_session_id:
        logger.error("Stream match requested without sessionId: payload=%s", payload)
        logger.error("Returning non-2xx response for missing stream match sessionId: status=400")
        return JsonResponse({"error": "sessionId is required."}, status=400)

    stream = STREAM_SESSIONS.get(requested_session_id)
    if stream is None and STREAM_SESSIONS:
        matched_session = next(iter(STREAM_SESSIONS.values()))
        stream = StreamSession(
            session_id=requested_session_id,
            upstream_stream_id=matched_session.upstream_stream_id or matched_session.session_id,
            whip_url=matched_session.whip_url,
            whep_url=matched_session.whep_url,
            output_video_url=matched_session.output_video_url,
            initial_whep_url=matched_session.initial_whep_url,
            whep_resource_url=matched_session.whep_resource_url,
        )
        STREAM_SESSIONS[requested_session_id] = stream

    if stream is None:
        logger.error("No stream match available for requested session '%s'.", requested_session_id)
        logger.error("Returning non-2xx response for missing stream match: status=404")
        return JsonResponse({"error": "No stream match found."}, status=404)

    return JsonResponse(
        {
            "sessionId": stream.session_id,
            "whipUrl": request.build_absolute_uri(f"/streams/{stream.session_id}/whip"),
            "whepUrl": request.build_absolute_uri(f"/streams/{stream.session_id}/whep"),
            "outputVideoUrl": stream.output_video_url,
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def whip_proxy(request: HttpRequest, session_id: str) -> HttpResponse:
    stream = STREAM_SESSIONS.get(session_id)
    if stream is None:
        logger.error("WHIP requested for unknown stream session '%s'.", session_id)
        logger.error("Returning non-2xx response for missing WHIP session: status=404")
        return HttpResponse("WHIP session not found.", status=404)

    request_context = _request_debug_context(request)
    logger.info(
        "Forwarding WHIP request for session %s with context=%s",
        session_id,
        request_context,
    )

    try:
        with httpx.Client(timeout=30) as client:
            upstream = client.post(
                stream.whip_url,
                headers={"Content-Type": request.headers.get("Content-Type", "application/sdp")},
                content=request.body,
            )
    except httpx.HTTPError as exc:
        logger.error(
            "WHIP proxy failed for session '%s': %s. request_context=%s upstream_url=%s",
            session_id,
            exc,
            request_context,
            stream.whip_url,
            exc_info=True,
        )
        logger.error("Returning non-2xx response for WHIP upstream failure: status=502")
        return HttpResponse("WHIP upstream unavailable.", status=502)

    logger.info(
        "WHIP upstream responded for session %s with details=%s",
        session_id,
        _upstream_debug_context(upstream),
    )

    response = HttpResponse(
        upstream.text,
        status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/sdp"),
    )
    if upstream.status_code >= 400:
        logger.error(
            "WHIP upstream returned non-2xx for session '%s': status=%s body=%s",
            session_id,
            upstream.status_code,
            upstream.text[:500],
        )
        logger.error(
            "Returning non-2xx response for WHIP upstream status passthrough: status=%s",
            upstream.status_code,
        )
    playback_url = upstream.headers.get("livepeer-playback-url") or upstream.headers.get(
        "Livepeer-Playback-Url"
    )
    if upstream.status_code >= 400:
        return response

    if not playback_url:
        logger.error(
            "WHIP upstream response for session '%s' is missing livepeer-playback-url; "
            "unable to determine the true WHEP upstream target.",
            session_id,
        )
        logger.error("Returning non-2xx response for missing WHIP playback header: status=502")
        return HttpResponse("WHIP upstream did not provide a playback URL.", status=502)

    normalized_playback_url = playback_url
    updated_stream = StreamSession(
        session_id=stream.session_id,
        upstream_stream_id=stream.upstream_stream_id or stream.session_id,
        whip_url=stream.whip_url,
        whep_url=normalized_playback_url,
        output_video_url=stream.output_video_url or normalized_playback_url,
        initial_whep_url=stream.initial_whep_url,
    )
    STREAM_SESSIONS[session_id] = updated_stream
    for candidate_id, candidate_stream in list(STREAM_SESSIONS.items()):
        if candidate_stream.whip_url == stream.whip_url:
            STREAM_SESSIONS[candidate_id] = StreamSession(
                session_id=candidate_stream.session_id,
                upstream_stream_id=(
                    candidate_stream.upstream_stream_id
                    or stream.upstream_stream_id
                    or stream.session_id
                ),
                whip_url=candidate_stream.whip_url,
                whep_url=normalized_playback_url,
                output_video_url=(
                    candidate_stream.output_video_url
                    or stream.output_video_url
                    or normalized_playback_url
                ),
                initial_whep_url=candidate_stream.initial_whep_url or stream.initial_whep_url,
                whep_resource_url="",
            )
    response["livepeer-playback-url"] = request.build_absolute_uri(f"/streams/{session_id}/whep")
    location = upstream.headers.get("location")
    if location:
        response["location"] = request.build_absolute_uri(f"/streams/{session_id}/whep/resource")
    return response


@csrf_exempt
@require_http_methods(["POST"])
def whep_proxy(request: HttpRequest, session_id: str) -> HttpResponse:
    stream = STREAM_SESSIONS.get(session_id)
    if stream is None or not stream.whep_url:
        logger.error(
            "WHEP POST requested for unknown or incomplete stream session '%s'.",
            session_id,
        )
        logger.error("Returning non-2xx response for missing WHEP session: status=404")
        return HttpResponse("WHEP session not found.", status=404)

    return _proxy_whep_request(request=request, session_id=session_id, method="POST")


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def whep_resource_proxy(request: HttpRequest, session_id: str) -> HttpResponse:
    stream = STREAM_SESSIONS.get(session_id)
    if stream is None or not stream.whep_url:
        logger.error(
            "WHEP resource request for unknown or incomplete stream session '%s'.",
            session_id,
        )
        logger.error("Returning non-2xx response for missing WHEP resource: status=404")
        return HttpResponse("WHEP resource not found.", status=404)

    return _proxy_whep_request(request=request, session_id=session_id, method=request.method)


def _proxy_whep_request(request: HttpRequest, session_id: str, method: str) -> HttpResponse:
    stream = STREAM_SESSIONS[session_id]
    upstream_url = _whep_upstream_url(stream, method)
    request_context = _request_debug_context(request)
    logger.info(
        "Forwarding WHEP request for session %s using method %s "
        "with request_context=%s upstream_url=%s",
        session_id,
        method,
        request_context,
        upstream_url,
    )

    if not upstream_url:
        logger.error(
            "WHEP request for session '%s' has no upstream URL. request_context=%s",
            session_id,
            request_context,
        )
        logger.error("Returning non-2xx response for missing WHEP upstream URL: status=404")
        return HttpResponse("WHEP session not found.", status=404)

    try:
        with httpx.Client(timeout=WHEP_PROXY_TIMEOUT) as client:
            upstream = client.request(
                method,
                upstream_url,
                headers={
                    "Content-Type": request.headers.get(
                        "Content-Type",
                        "application/trickle-ice-sdpfrag",
                    )
                },
                content=request.body,
            )
    except httpx.HTTPError as exc:
        logger.error(
            "WHEP proxy failed for session '%s' using method %s against upstream %s: %s. "
            "request_context=%s timeout=%s",
            session_id,
            method,
            upstream_url,
            exc,
            request_context,
            WHEP_PROXY_TIMEOUT,
            exc_info=True,
        )
        logger.error("Returning non-2xx response for WHEP upstream failure: status=502")
        return HttpResponse("WHEP upstream unavailable.", status=502)

    logger.info(
        "WHEP upstream responded for session %s using method %s against upstream %s "
        "with details=%s",
        session_id,
        method,
        upstream_url,
        _upstream_debug_context(upstream),
    )

    response = HttpResponse(
        upstream.text,
        status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/sdp"),
    )
    if upstream.status_code >= 400:
        logger.error(
            "WHEP upstream returned non-2xx for session '%s' using method %s: status=%s body=%s",
            session_id,
            method,
            upstream.status_code,
            upstream.text[:500],
        )
        logger.error(
            "Returning non-2xx response for WHEP upstream status passthrough: status=%s",
            upstream.status_code,
        )
    location = upstream.headers.get("location")
    if location:
        STREAM_SESSIONS[session_id] = StreamSession(
            session_id=stream.session_id,
            upstream_stream_id=stream.upstream_stream_id,
            whip_url=stream.whip_url,
            whep_url=stream.whep_url,
            output_video_url=stream.output_video_url,
            initial_whep_url=stream.initial_whep_url,
            whep_resource_url=location,
        )
        response["location"] = request.build_absolute_uri(f"/streams/{session_id}/whep/resource")
    return response


def _normalize_browser_session_id(request: HttpRequest) -> str:
    requested_session_id = str(request.POST.get("browser_session_id") or "").strip()
    if requested_session_id:
        return requested_session_id

    generated_session_id = str(uuid.uuid4())
    logger.error(
        "Start request missing browser_session_id; generated fallback session '%s'.",
        generated_session_id,
    )
    return generated_session_id


def _handle_start(
    request: HttpRequest,
    context: dict[str, object],
    *,
    page: str,
) -> tuple[dict[str, object], int]:
    if page == "music":
        return _handle_music_start(request, context)
    return _handle_book_start(request, context)


def _handle_book_start(
    request: HttpRequest,
    context: dict[str, object],
) -> tuple[dict[str, object], int]:
    form = BookRequestForm(request.POST)
    context["book_form"] = form

    if not form.is_valid():
        logger.error("Invalid book start form submission: %s", form.errors)
        context["error"] = "Please provide both a book query and a prompt."
        return context, 400

    query = form.cleaned_data["book_query"].strip()
    prompt = form.cleaned_data["daydream_prompt"].strip()
    browser_session_id = _normalize_browser_session_id(request)

    try:
        experience = NarrativeClientFactory.create().build_experience(query)
        livepeer_stream_session = DaydreamClient().create_livepeer_stream_session(prompt)
    except UpstreamServiceError as exc:
        logger.error("Failed to start book stream for query '%s': %s", query, exc)
        context["error"] = str(exc)
        logger.error("Returning non-2xx response for book stream upstream failure: status=502")
        return context, 502

    _store_stream_session(browser_session_id, livepeer_stream_session)
    prepared_stream = {
        "mode": "book",
        "narrative_mode_provider": context["narrative_mode_provider"],
        "title": experience.title,
        "author": experience.author,
        "prompt": prompt,
        "pdf_url": experience.pdf_url,
        "source_audio_path": str(getattr(experience, "source_audio_path", "") or ""),
        "source_audio_mime_type": str(getattr(experience, "source_audio_mime_type", "") or ""),
        "source_video_url": str(getattr(experience, "source_video_url", "") or ""),
        "storage_path": experience.storage_path,
        "chunks": [
            {
                "chapter_title": chunk.chapter_title,
                "chunk_index": chunk.chunk_index,
                "text": chunk.text,
            }
            for chunk in experience.chunks
        ],
    }
    request.session["prepared_stream"] = prepared_stream
    context["stream"] = prepared_stream
    context["message"] = (
        "Narrative source found, media prepared, and playback is ready. "
        "The frontend can now reconnect to the matching Livepeer stream session."
    )
    return context, 200


def _handle_music_start(
    request: HttpRequest,
    context: dict[str, object],
) -> tuple[dict[str, object], int]:
    form = MusicRequestForm(request.POST)
    context["music_form"] = form

    if not form.is_valid():
        logger.error("Invalid music start form submission: %s", form.errors)
        context["error"] = "Please provide both a music title and a prompt."
        return context, 400

    query = form.cleaned_data["music_query"].strip()
    prompt = form.cleaned_data["daydream_prompt"].strip()
    browser_session_id = _normalize_browser_session_id(request)

    try:
        track = MusicSearchClient().search_track(query)
        livepeer_stream_session = DaydreamClient().create_livepeer_stream_session(prompt)
    except UpstreamServiceError as exc:
        logger.error("Failed to start music stream for query '%s': %s", query, exc)
        context["error"] = str(exc)
        logger.error("Returning non-2xx response for music stream upstream failure: status=502")
        return context, 502

    _store_stream_session(browser_session_id, livepeer_stream_session)
    prepared_stream = {
        "mode": "music",
        "title": track.title,
        "author": track.artist,
        "prompt": prompt,
        "storage_path": track.provider,
        "chunks": [],
    }
    request.session["prepared_stream"] = prepared_stream
    context["stream"] = prepared_stream
    context["music_track"] = {
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "cover_image_url": track.cover_image_url,
        "external_url": track.external_url,
        "preview_url": track.preview_url,
        "provider": track.provider,
    }
    context["message"] = (
        "Spotify track found and the visual music stream is ready. "
        "Connect WHIP/WHEP to publish the canvas while the song playback drives the session."
    )
    return context, 200


def _store_stream_session(browser_session_id: str, livepeer_stream_session: StreamSession) -> None:
    initial_output_video_url = (
        livepeer_stream_session.output_video_url or livepeer_stream_session.whep_url
    )
    STREAM_SESSIONS[browser_session_id] = StreamSession(
        session_id=browser_session_id,
        upstream_stream_id=livepeer_stream_session.session_id,
        whip_url=livepeer_stream_session.whip_url,
        whep_url=livepeer_stream_session.whep_url,
        output_video_url=initial_output_video_url,
        initial_whep_url=livepeer_stream_session.whep_url,
    )
    STREAM_SESSIONS[livepeer_stream_session.session_id] = StreamSession(
        session_id=livepeer_stream_session.session_id,
        upstream_stream_id=livepeer_stream_session.session_id,
        whip_url=livepeer_stream_session.whip_url,
        whep_url=livepeer_stream_session.whep_url,
        output_video_url=initial_output_video_url,
        initial_whep_url=livepeer_stream_session.whep_url,
    )


@csrf_exempt
@require_POST
def stream_prompt(request: HttpRequest, session_id: str) -> JsonResponse:
    stream = STREAM_SESSIONS.get(session_id)
    if stream is None or not stream.upstream_stream_id:
        logger.error(
            "Prompt update requested for unknown or incomplete stream session '%s'.", session_id
        )
        logger.error("Returning non-2xx response for missing prompt update session: status=404")
        return JsonResponse({"error": "Stream session not found."}, status=404)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        logger.error("Invalid prompt update payload for session '%s': %s", session_id, exc)
        logger.error("Returning non-2xx response for invalid prompt update payload: status=400")
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        logger.error(
            "Prompt update requested without prompt text for session '%s': payload=%s",
            session_id,
            payload,
        )
        logger.error("Returning non-2xx response for missing prompt text: status=400")
        return JsonResponse({"error": "prompt is required."}, status=400)

    try:
        PromptStreamUpdater().update_prompt(
            upstream_stream_id=stream.upstream_stream_id,
            prompt=prompt,
        )
    except UpstreamServiceError as exc:
        logger.error("Prompt update failed for session '%s': %s", session_id, exc)
        logger.error("Returning non-2xx response for prompt update upstream failure: status=502")
        return JsonResponse({"error": str(exc)}, status=502)

    return JsonResponse({"message": "Prompt updated."})


@csrf_exempt
@require_POST
def stream_story_prompt(request: HttpRequest, session_id: str) -> JsonResponse:
    stream = STREAM_SESSIONS.get(session_id)
    if stream is None or not stream.upstream_stream_id:
        logger.error(
            "Story prompt update requested for unknown or incomplete stream session '%s'.",
            session_id,
        )
        logger.error("Returning non-2xx response for missing story prompt session: status=404")
        return JsonResponse({"error": "Stream session not found."}, status=404)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        logger.error("Invalid story prompt payload for session '%s': %s", session_id, exc)
        logger.error("Returning non-2xx response for invalid story prompt payload: status=400")
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    video_id = str(payload.get("videoId") or "").strip()
    fallback_text = str(payload.get("fallbackText") or "").strip()
    current_seconds_raw = payload.get("currentSeconds")
    delta_seconds = float(os.getenv("UPDATE_STORY_PROMPT_DELTA_SECONDS", "10"))
    if not video_id:
        logger.error(
            "Story prompt update requested without videoId for session '%s': payload=%s",
            session_id,
            payload,
        )
        logger.error("Returning non-2xx response for missing story prompt videoId: status=400")
        return JsonResponse({"error": "videoId is required."}, status=400)

    try:
        current_seconds = float(current_seconds_raw)
    except (TypeError, ValueError) as exc:
        logger.error(
            "Story prompt update requested with invalid currentSeconds for session '%s': %s",
            session_id,
            exc,
        )
        logger.error(
            "Returning non-2xx response for invalid story prompt currentSeconds: status=400"
        )
        return JsonResponse({"error": "currentSeconds must be a number."}, status=400)

    try:
        prompt, source_text = YouTubeStoryPromptClient().build_story_prompt(
            video_id=video_id,
            current_seconds=current_seconds,
            delta_seconds=delta_seconds,
            fallback_text=fallback_text,
        )
        PromptStreamUpdater().update_prompt(
            upstream_stream_id=stream.upstream_stream_id,
            prompt=prompt,
        )
    except UpstreamServiceError as exc:
        logger.error("Story prompt update failed for session '%s': %s", session_id, exc)
        logger.error("Returning non-2xx response for story prompt upstream failure: status=502")
        return JsonResponse({"error": str(exc)}, status=502)

    return JsonResponse(
        {
            "message": "Story prompt updated.",
            "prompt": prompt,
            "sourceText": source_text,
            "deltaSeconds": delta_seconds,
        }
    )


@require_GET
def coqui_tts(request: HttpRequest) -> HttpResponse:
    session_id = str(request.GET.get("sessionId") or "").strip()
    chunk_index_raw = str(request.GET.get("chunkIndex") or "").strip()

    if not session_id or not chunk_index_raw:
        logger.error(
            "Coqui TTS requested without required parameters. session_id=%s chunk_index=%s",
            session_id,
            chunk_index_raw,
        )
        logger.error("Returning non-2xx response for missing Coqui parameters: status=400")
        return HttpResponse("sessionId and chunkIndex are required.", status=400)

    try:
        chunk_index = int(chunk_index_raw)
    except ValueError as exc:
        logger.error("Coqui TTS requested with invalid chunk index '%s': %s", chunk_index_raw, exc)
        logger.error("Returning non-2xx response for invalid Coqui chunk index: status=400")
        return HttpResponse("chunkIndex must be an integer.", status=400)

    stream_context = STREAM_SESSIONS.get(session_id)
    if stream_context is None:
        logger.error("Coqui TTS requested for unknown stream session '%s'.", session_id)
        logger.error("Returning non-2xx response for missing Coqui stream session: status=404")
        return HttpResponse("Stream session not found.", status=404)

    home_stream = request.session.get("prepared_stream")
    if not isinstance(home_stream, dict):
        logger.error(
            "Coqui TTS requested without a prepared_stream in session. session_id=%s",
            session_id,
        )
        logger.error("Returning non-2xx response for missing prepared stream context: status=404")
        return HttpResponse("Prepared stream context not found.", status=404)
    chunks = list(home_stream.get("chunks", [])) if isinstance(home_stream, dict) else []
    if chunk_index < 1 or chunk_index > len(chunks):
        logger.error(
            (
                "Coqui TTS requested for unavailable chunk. "
                "session_id=%s chunk_index=%s total_chunks=%s"
            ),
            session_id,
            chunk_index,
            len(chunks),
        )
        logger.error("Returning non-2xx response for missing Coqui chunk: status=404")
        return HttpResponse("Chunk not found for this stream session.", status=404)

    source_audio_path = str(home_stream.get("source_audio_path") or "").strip()
    source_audio_mime_type = str(home_stream.get("source_audio_mime_type") or "").strip()
    if source_audio_path:
        if chunk_index != 1:
            logger.error(
                "Source media playback requested for unsupported chunk index. "
                "session_id=%s chunk_index=%s",
                session_id,
                chunk_index,
            )
            logger.error(
                "Returning non-2xx response for unsupported source media chunk: status=404"
            )
            return HttpResponse("Requested chunk not found.", status=404)
        result = type(
            "PreparedSourceAudioResult",
            (),
            {"audio_path": source_audio_path, "mime_type": source_audio_mime_type or "video/mp4"},
        )()
    else:
        chunk = home_stream["chunks"][chunk_index - 1]
        try:
            result = CoquiTtsClient().synthesize(
                session_id=session_id,
                chunk_index=chunk_index,
                text=str(chunk.get("text") or ""),
            )
        except UpstreamServiceError as exc:
            logger.error(
                "Coqui TTS generation failed for session '%s' chunk %s: %s",
                session_id,
                chunk_index,
                exc,
            )
            logger.error("Returning non-2xx response for Coqui synthesis failure: status=502")
            return HttpResponse(str(exc), status=502)

    audio_file_path = str(result.audio_path).strip()
    if not audio_file_path or not Path(audio_file_path).exists():
        logger.error(
            "Prepared audio file is missing for session '%s' chunk %s: %s",
            session_id,
            chunk_index,
            audio_file_path,
        )
        logger.error("Returning non-2xx response for missing prepared audio file: status=404")
        return HttpResponse("Prepared audio file not found.", status=404)

    return FileResponse(open(result.audio_path, "rb"), content_type=result.mime_type)
