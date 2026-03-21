import json
import logging
import uuid

import httpx
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import BookRequestForm
from .services import (
    DaydreamClient,
    StreamSession,
    UpstreamServiceError,
    WebResearchNarrativeClient,
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


def home(request: HttpRequest) -> HttpResponse:
    context: dict[str, object] = {
        "book_form": BookRequestForm(),
        "message": "",
        "error": "",
        "stream": None,
    }

    if request.method == "POST":
        try:
            context, status_code = _handle_start(request, context)
            if status_code >= 400:
                logger.error("Returning non-2xx response for start action: status=%s", status_code)
            return render(request, "audiobook/home.html", context, status=status_code)
        except Exception as exc:
            logger.error("Unhandled error while processing home POST: %s", exc, exc_info=True)
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
        logger.error(
            "Returning non-2xx response for missing WHIP playback header: status=502"
        )
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
    response["livepeer-playback-url"] = request.build_absolute_uri(
        f"/streams/{session_id}/whep"
    )
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
) -> tuple[dict[str, object], int]:
    form = BookRequestForm(request.POST)
    context["book_form"] = form

    if not form.is_valid():
        logger.error("Invalid start form submission: %s", form.errors)
        context["error"] = "Please provide both a book query and a prompt."
        return context, 400

    query = form.cleaned_data["book_query"].strip()
    prompt = form.cleaned_data["daydream_prompt"].strip()
    browser_session_id = _normalize_browser_session_id(request)

    try:
        experience = WebResearchNarrativeClient().build_experience(query)
        livepeer_stream_session = DaydreamClient().create_livepeer_stream_session(prompt)
    except UpstreamServiceError as exc:
        logger.error("Failed to start stream for query '%s': %s", query, exc)
        context["error"] = str(exc)
        logger.error("Returning non-2xx response for start stream upstream failure: status=502")
        return context, 502

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

    context["stream"] = {
        "title": experience.title,
        "author": experience.author,
        "prompt": prompt,
        "pdf_url": experience.pdf_url,
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
    context["message"] = (
        "PDF found, downloaded, chunked, and prepared for browser TTS. "
        "The frontend can now reconnect to the matching Livepeer stream session."
    )
    return context, 200
