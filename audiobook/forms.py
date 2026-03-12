from django import forms


class BookRequestForm(forms.Form):
    book_query = forms.CharField(max_length=255, label="Book request")
    daydream_prompt = forms.CharField(
        max_length=500,
        required=False,
        label="Daydream prompt",
        help_text='If empty, defaults to "go with the flow".',
    )


class PromptUpdateForm(forms.Form):
    session_id = forms.CharField(max_length=255)
    daydream_prompt = forms.CharField(max_length=500, required=False)
