from django import forms


class BookRequestForm(forms.Form):
    book_query = forms.CharField(
        max_length=255,
        label="Book request",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Try: Pride and Prejudice, Dune, The Odyssey...",
            }
        ),
    )
    daydream_prompt = forms.CharField(
        max_length=500,
        required=False,
        label="Daydream prompt",
        help_text=(
            'If empty, defaults to "go with the flow" or the first '
            'WEB_RESEARCH_TTS chunk prompt.'
        ),
        widget=forms.TextInput(
            attrs={
                "placeholder": "moody cinematic dreamscape with dramatic light",
            }
        ),
    )


class PromptUpdateForm(forms.Form):
    session_id = forms.CharField(max_length=255)
    daydream_prompt = forms.CharField(max_length=500, required=False)
