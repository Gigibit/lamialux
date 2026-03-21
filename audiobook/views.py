import json
import logging

import httpx
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import BookRequestForm
from .services import StreamSession, UpstreamServiceError, WebResearchNarrativeClient

logger = logging.getLogger("audiobook")
STREAM_SESSIONS: dict[str, StreamSession] = {}
WHEP_PROXY_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=30.0, pool=5.0)


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
            whip_url=matched_session.whip_url,
            whep_url=matched_session.whep_url,
            output_video_url=matched_session.output_video_url,
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

    try:
        with httpx.Client(timeout=30) as client:
            upstream = client.post(
                stream.whip_url,
                headers={"Content-Type": request.headers.get("Content-Type", "application/sdp")},
                content=request.body,
            )
    except httpx.HTTPError as exc:
        logger.error("WHIP proxy failed for session '%s': %s", session_id, exc, exc_info=True)
        logger.error("Returning non-2xx response for WHIP upstream failure: status=502")
        return HttpResponse("WHIP upstream unavailable.", status=502)

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
            upstream.text,
        )
        logger.error(
            "Returning non-2xx response for WHIP upstream status passthrough: status=%s",
            upstream.status_code,
        )
    playback_url = upstream.headers.get("livepeer-playback-url") or upstream.headers.get(
        "Livepeer-Playback-Url"
    )
    if playback_url:
        STREAM_SESSIONS[session_id] = StreamSession(
            session_id=stream.session_id,
            whip_url=stream.whip_url,
            whep_url=playback_url,
            output_video_url=playback_url,
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
    try:
        with httpx.Client(timeout=WHEP_PROXY_TIMEOUT) as client:
            upstream = client.request(
                method,
                stream.whep_url,
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
            "WHEP proxy failed for session '%s' using method %s: %s",
            session_id,
            method,
            exc,
            exc_info=True,
        )
        logger.error("Returning non-2xx response for WHEP upstream failure: status=502")
        return HttpResponse("WHEP upstream unavailable.", status=502)

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
            upstream.text,
        )
        logger.error(
            "Returning non-2xx response for WHEP upstream status passthrough: status=%s",
            upstream.status_code,
        )
    location = upstream.headers.get("location")
    if location:
        response["location"] = request.build_absolute_uri(f"/streams/{session_id}/whep/resource")
    return response


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

    try:
        experience = WebResearchNarrativeClient().build_experience(query)
    except UpstreamServiceError as exc:
        logger.error("Failed to start stream for query '%s': %s", query, exc)
        context["error"] = str(exc)
        logger.error("Returning non-2xx response for start stream upstream failure: status=502")
        return context, 502

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
        "The frontend will request a matching stream session when you connect."
    )
    return context, 200
