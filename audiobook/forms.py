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


class MusicRequestForm(forms.Form):
    music_query = forms.CharField(
        max_length=255,
        label="Music title",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Try: Teardrop, Time, Midnight City...",
            }
        ),
    )
    daydream_prompt = forms.CharField(
        max_length=500,
        label="Prompt",
        widget=forms.TextInput(
            attrs={
                "placeholder": "neon pulse reacting to the rhythm",
            }
        ),
    )
