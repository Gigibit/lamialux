import logging
from dataclasses import asdict

import httpx
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .forms import BookRequestForm, PromptUpdateForm
from .services import (
    DaydreamClient,
    NarrativeConfig,
    NarrativeMode,
    OpenAITTSClient,
    StorytelClient,
    StreamSession,
    UpstreamServiceError,
    WebResearchNarrativeClient,
)

logger = logging.getLogger("audiobook")
STREAM_SESSIONS: dict[str, StreamSession] = {}


def home(request: HttpRequest) -> HttpResponse:
    context: dict[str, object] = {
        "book_form": BookRequestForm(),
        "prompt_form": PromptUpdateForm(),
        "message": "",
        "error": "",
        "stream": None,
        "narrative_mode": NarrativeConfig.mode(),
    }

    if request.method == "POST":
        action = request.POST.get("action", "start")
        try:
            if action == "start":
                context, status_code = _handle_start(request, context)
                if status_code >= 400:
                    logger.error(
                        "Returning non-2xx response for start action: status=%s",
                        status_code,
                    )
                return render(request, "audiobook/home.html", context, status=status_code)
            if action == "update_prompt":
                context, status_code = _handle_prompt_update(request, context)
                if status_code >= 400:
                    logger.error(
                        "Returning non-2xx response for update_prompt action: status=%s",
                        status_code,
                    )
                return render(request, "audiobook/home.html", context, status=status_code)

            logger.error("Invalid action received: %s", action)
            context["error"] = "Invalid action requested."
            logger.error("Returning non-2xx response for invalid action: status=400")
            return render(request, "audiobook/home.html", context, status=400)
        except Exception as exc:
            logger.error(
                "Unhandled error while processing action '%s': %s",
                action,
                exc,
                exc_info=True,
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
    upstream_url = stream.whep_url
    if method in {"PATCH", "DELETE"}:
        upstream_url = stream.whep_url

    try:
        with httpx.Client(timeout=30) as client:
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
    location = upstream.headers.get("location")
    if location:
        response["location"] = request.build_absolute_uri(f"/streams/{session_id}/whep/resource")
    return response


@csrf_exempt
@require_http_methods(["POST"])
def narrate(request: HttpRequest) -> HttpResponse:
    text = request.POST.get("text", "").strip()
    voice = request.POST.get("voice", "alloy").strip()
    if not text:
        logger.error("Narration requested without text payload.")
        logger.error("Returning non-2xx response for invalid narration request: status=400")
        return JsonResponse({"error": "Text is required."}, status=400)

    try:
        audio_bytes = OpenAITTSClient().synthesize(text=text, voice=voice)
    except UpstreamServiceError as exc:
        logger.error("Narration synthesis failed for voice '%s': %s", voice, exc)
        logger.error("Returning non-2xx response for narration synthesis failure: status=502")
        return JsonResponse({"error": str(exc)}, status=502)

    return HttpResponse(audio_bytes, content_type="audio/mpeg")


def _handle_start(
    request: HttpRequest,
    context: dict[str, object],
) -> tuple[dict[str, object], int]:
    form = BookRequestForm(request.POST)
    context["book_form"] = form

    if not form.is_valid():
        logger.error("Invalid start form submission: %s", form.errors)
        context["error"] = "Please provide a valid book request."
        return context, 400

    prompt = (form.cleaned_data["daydream_prompt"] or "go with the flow").strip()
    query = form.cleaned_data["book_query"].strip()
    mode = NarrativeConfig.mode()

    daydream = DaydreamClient()

    try:
        if mode == NarrativeMode.WEB_RESEARCH_TTS:
            experience = WebResearchNarrativeClient().build_experience(query)
            custom_prompt = form.cleaned_data["daydream_prompt"].strip()
            opening_prompt = prompt if custom_prompt else experience.chunks[0].prompt
            stream_session = daydream.create_livepeer_stream_session(prompt=opening_prompt)
            STREAM_SESSIONS[stream_session.session_id] = stream_session
            context["stream"] = {
                "title": experience.title,
                "author": experience.author,
                "cover_url": experience.cover_url,
                "video_url": request.build_absolute_uri(
                    f"/streams/{stream_session.session_id}/whep"
                ),
                "session_id": stream_session.session_id,
                "prompt": opening_prompt,
                "mode": mode,
                "summary": experience.summary,
                "pdf_url": experience.pdf_url,
                "storage_path": experience.storage_path,
                "chunks": [
                    {
                        "chapter_title": chunk.chapter_title,
                        "chunk_index": chunk.chunk_index,
                        "text": chunk.text,
                        "prompt": chunk.prompt,
                    }
                    for chunk in experience.chunks
                ],
                "stream_session": asdict(stream_session),
            }
            context["message"] = (
                "Livepeer WHIP/WHEP narrative initialized. The selected voice will read the "
                "audiobook in-browser, publish that audio plus the generated canvas over WHIP, "
                "and playback through WHEP."
            )
        else:
            book = StorytelClient().find_audiobook(query)
            stream_session = daydream.create_livepeer_stream_session(prompt=prompt)
            STREAM_SESSIONS[stream_session.session_id] = stream_session
            context["stream"] = {
                "title": book.title,
                "author": book.author,
                "cover_url": book.cover_url,
                "video_url": request.build_absolute_uri(
                    f"/streams/{stream_session.session_id}/whep"
                ),
                "session_id": stream_session.session_id,
                "prompt": prompt,
                "mode": mode,
                "summary": "",
                "pdf_url": "",
                "storage_path": "",
                "audio_stream_url": book.stream_url,
                "chunks": [
                    {
                        "chapter_title": "Opening",
                        "chunk_index": 1,
                        "text": (
                            f"Now reading {book.title} by {book.author}. "
                            "Use the voice controls to publish narration into the Daydream canvas."
                        ),
                        "prompt": prompt,
                    }
                ],
                "stream_session": asdict(stream_session),
            }
            context["message"] = (
                "Livepeer WHIP/WHEP stream initialized. Use the voice controls to narrate the "
                "audiobook into the canvas and watch the generated playback below."
            )
    except UpstreamServiceError as exc:
        logger.error("Failed to start stream for query '%s' in mode %s: %s", query, mode, exc)
        context["error"] = str(exc)
        return context, 502

    context["prompt_form"] = PromptUpdateForm(
        initial={"session_id": context["stream"]["session_id"]}
    )
    return context, 200


def _handle_prompt_update(
    request: HttpRequest,
    context: dict[str, object],
) -> tuple[dict[str, object], int]:
    form = PromptUpdateForm(request.POST)
    context["prompt_form"] = form

    if not form.is_valid():
        logger.error("Invalid prompt update submission: %s", form.errors)
        context["error"] = "Prompt update failed."
        return context, 400

    session_id = form.cleaned_data["session_id"]
    prompt = (form.cleaned_data["daydream_prompt"] or "go with the flow").strip()

    try:
        DaydreamClient().update_prompt(session_id=session_id, prompt=prompt)
    except UpstreamServiceError as exc:
        logger.error("Prompt update failed for session %s: %s", session_id, exc)
        context["error"] = str(exc)
        return context, 502

    context["message"] = "Prompt updated successfully."
    context["stream"] = {
        "session_id": session_id,
        "video_url": request.POST.get("video_url", ""),
        "title": request.POST.get("title", ""),
        "author": request.POST.get("author", ""),
        "cover_url": request.POST.get("cover_url", ""),
        "prompt": prompt,
        "mode": request.POST.get("mode", NarrativeConfig.mode()),
        "summary": request.POST.get("summary", ""),
        "pdf_url": request.POST.get("pdf_url", ""),
        "storage_path": request.POST.get("storage_path", ""),
        "chunks": [],
    }
    return context, 200
