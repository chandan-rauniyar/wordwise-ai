from django import forms

from .models import Example, Word


class WordForm(forms.ModelForm):
    """Add or edit a word. The slug is made from the word, so it is not asked for."""

    class Meta:
        model = Word
        fields = ["text", "topic", "part_of_speech", "difficulty", "definition"]
        widgets = {
            "text": forms.TextInput(attrs={"placeholder": "admire", "autocapitalize": "none", "spellcheck": "false"}),
            "definition": forms.Textarea(attrs={"rows": 4}),
        }

    def clean_text(self):
        text = self.cleaned_data["text"].strip().lower()
        # unique check here too, so the message names the word the user typed
        clash = Word.objects.filter(text=text).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("This word is already in the library.")
        return text


class ExampleForm(forms.ModelForm):
    class Meta:
        model = Example
        fields = ["sentence"]
        widgets = {"sentence": forms.TextInput(attrs={"placeholder": "Write a sentence that uses the word"})}

    def clean_sentence(self):
        sentence = self.cleaned_data["sentence"].strip()
        if len(sentence) < 8:
            raise forms.ValidationError("Write a full sentence (at least 8 characters).")
        return sentence
