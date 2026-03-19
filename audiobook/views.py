import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from .forms import BookRequestForm, PromptUpdateForm
from .services import (
    DaydreamClient,
    NarrativeConfig,
    NarrativeMode,
    StorytelClient,
    UpstreamServiceError,
    WebResearchNarrativeClient,
)

logger = logging.getLogger("audiobook")


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
            stream = daydream.start_canvas_stream(
                audio_stream_url=experience.audio_stream_url,
                prompt=opening_prompt,
            )
            context["stream"] = {
                "title": experience.title,
                "author": experience.author,
                "cover_url": experience.cover_url,
                "video_url": stream["output_video_url"],
                "session_id": stream["session_id"],
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
            }
            context["message"] = (
                "Web research narrative initialized. The PDF was chunked, stored locally, "
                "and prepared for synchronized TTS + Daydream playback."
            )
        else:
            book = StorytelClient().find_audiobook(query)
            stream = daydream.start_canvas_stream(audio_stream_url=book.stream_url, prompt=prompt)
            context["stream"] = {
                "title": book.title,
                "author": book.author,
                "cover_url": book.cover_url,
                "video_url": stream["output_video_url"],
                "session_id": stream["session_id"],
                "prompt": prompt,
                "mode": mode,
                "summary": "",
                "pdf_url": "",
                "storage_path": "",
                "chunks": [],
            }
            context["message"] = "Stream initialized. You can update prompt below the video."
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
