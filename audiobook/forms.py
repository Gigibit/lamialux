from django import forms


class BookRequestForm(forms.Form):
    book_query = forms.CharField(
        max_length=255,
        label="Book",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Try: Pride and Prejudice, Dune, The Odyssey...",
            }
        ),
    )
    daydream_prompt = forms.CharField(
        max_length=500,
        label="Prompt",
        widget=forms.TextInput(
            attrs={
                "placeholder": "moody cinematic dreamscape with dramatic light",
            }
        ),
    )
