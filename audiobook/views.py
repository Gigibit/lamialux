import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from .forms import BookRequestForm, PromptUpdateForm
from .services import DaydreamClient, StorytelClient, UpstreamServiceError

logger = logging.getLogger("audiobook")


def home(request: HttpRequest) -> HttpResponse:
    context: dict[str, object] = {
        "book_form": BookRequestForm(),
        "prompt_form": PromptUpdateForm(),
        "message": "",
        "error": "",
        "stream": None,
    }

    if request.method == "POST":
        action = request.POST.get("action", "start")
        if action == "start":
            context = _handle_start(request, context)
        elif action == "update_prompt":
            context = _handle_prompt_update(request, context)
        else:
            logger.error("Invalid action received: %s", action)
            context["error"] = "Invalid action requested."

    return render(request, "audiobook/home.html", context)


def _handle_start(request: HttpRequest, context: dict[str, object]) -> dict[str, object]:
    form = BookRequestForm(request.POST)
    context["book_form"] = form

    if not form.is_valid():
        logger.error("Invalid start form submission: %s", form.errors)
        context["error"] = "Please provide a valid book request."
        return context

    prompt = (form.cleaned_data["daydream_prompt"] or "go with the flow").strip()
    query = form.cleaned_data["book_query"].strip()

    storytel = StorytelClient()
    daydream = DaydreamClient()

    try:
        book = storytel.find_audiobook(query)
        stream = daydream.start_canvas_stream(audio_stream_url=book.stream_url, prompt=prompt)
    except UpstreamServiceError as exc:
        logger.error("Failed to start stream for query '%s': %s", query, exc)
        context["error"] = str(exc)
        return context

    context["stream"] = {
        "title": book.title,
        "author": book.author,
        "cover_url": book.cover_url,
        "video_url": stream["output_video_url"],
        "session_id": stream["session_id"],
        "prompt": prompt,
    }
    context["prompt_form"] = PromptUpdateForm(initial={"session_id": stream["session_id"]})
    context["message"] = "Stream initialized. You can update prompt below the video."
    return context


def _handle_prompt_update(
    request: HttpRequest,
    context: dict[str, object],
) -> dict[str, object]:
    form = PromptUpdateForm(request.POST)
    context["prompt_form"] = form

    if not form.is_valid():
        logger.error("Invalid prompt update submission: %s", form.errors)
        context["error"] = "Prompt update failed."
        return context

    session_id = form.cleaned_data["session_id"]
    prompt = (form.cleaned_data["daydream_prompt"] or "go with the flow").strip()

    try:
        DaydreamClient().update_prompt(session_id=session_id, prompt=prompt)
    except UpstreamServiceError as exc:
        logger.error("Prompt update failed for session %s: %s", session_id, exc)
        context["error"] = str(exc)
        return context

    context["message"] = "Prompt updated successfully."
    context["stream"] = {
        "session_id": session_id,
        "video_url": request.POST.get("video_url", ""),
        "title": request.POST.get("title", ""),
        "author": request.POST.get("author", ""),
        "cover_url": request.POST.get("cover_url", ""),
    }
    return context
