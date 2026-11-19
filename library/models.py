"""Models of the word library.

Topic 1 --- * Word 1 --- * Example

* A Topic groups words (business, travel, ...).
* A Word belongs to exactly one Topic and has a difficulty level.
* An Example is one sentence that shows how a Word is used.
"""

from django.core.validators import MinLengthValidator, RegexValidator
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

DIFFICULTIES = ("EASY", "MEDIUM", "HARD")

# letters, with an inner hyphen or apostrophe allowed: "well-known", "o'clock"
word_validator = RegexValidator(
    regex=r"^[A-Za-z][A-Za-z'\-]*$",
    message="Use letters only (a hyphen or apostrophe is allowed inside the word).",
)


def make_unique_slug(model, value, instance):
    """slugify(value), with -2, -3 ... added until no other row of `model` has it."""
    base = slugify(value)[:60] or "item"
    slug = base
    number = 2
    others = model.objects.exclude(pk=instance.pk)
    while others.filter(slug=slug).exists():
        slug = f"{base}-{number}"
        number += 1
    return slug


class Topic(models.Model):
    name = models.CharField(max_length=60, unique=True)
    slug = models.SlugField(max_length=70, unique=True, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = make_unique_slug(Topic, self.name, self)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("library:topic_detail", kwargs={"slug": self.slug})


class Word(models.Model):
    PART_OF_SPEECH = [
        ("noun", "Noun"),
        ("verb", "Verb"),
        ("adjective", "Adjective"),
        ("adverb", "Adverb"),
        ("other", "Other"),
    ]
    DIFFICULTY = [("EASY", "Easy"), ("MEDIUM", "Medium"), ("HARD", "Hard")]

    text = models.CharField(
        max_length=40,
        unique=True,
        validators=[MinLengthValidator(2), word_validator],
        help_text="The word itself, for example: admire",
    )
    slug = models.SlugField(max_length=60, unique=True, blank=True)
    # PROTECT: a topic that still has words cannot be deleted by accident.
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="words")
    part_of_speech = models.CharField(max_length=10, choices=PART_OF_SPEECH, default="noun")
    difficulty = models.CharField(max_length=6, choices=DIFFICULTY, default="EASY")
    definition = models.TextField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["text"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(difficulty__in=DIFFICULTIES),
                name="word_difficulty_valid",
            ),
        ]

    def __str__(self):
        return self.text

    def save(self, *args, **kwargs):
        self.text = self.text.strip().lower()
        if not self.slug:
            self.slug = make_unique_slug(Word, self.text, self)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("library:word_detail", kwargs={"slug": self.slug})


class Example(models.Model):
    word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name="examples")
    sentence = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.word.text}: {self.sentence[:40]}"
