"""Views of the word library: list, search, filter, detail, CRUD and a JSON API."""

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponseServerError, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import ExampleForm, WordForm
from .models import DIFFICULTIES, Example, Topic, Word

PAGE_SIZE = 9

# the short notice shown after a redirect (?done=created)
DONE_MESSAGES = {
    "created": "The word was added.",
    "updated": "The word was updated.",
    "deleted": "The word was deleted.",
    "example": "The example was added.",
    "example_deleted": "The example was removed.",
}


def _done_message(request):
    return DONE_MESSAGES.get(request.GET.get("done", ""))


def _search(queryset, request):
    """Apply ?q= ?topic= ?difficulty= to a queryset of words."""
    q = request.GET.get("q", "").strip()[:60]
    topic = request.GET.get("topic", "").strip()
    difficulty = request.GET.get("difficulty", "").strip().upper()
    if q:
        queryset = queryset.filter(Q(text__icontains=q) | Q(definition__icontains=q))
    if topic:
        queryset = queryset.filter(topic__slug=topic)
    if difficulty in DIFFICULTIES:
        queryset = queryset.filter(difficulty=difficulty)
    return queryset, q, topic, difficulty


def _listing(request, queryset, heading, intro=""):
    """Shared by every page that shows a list of words."""
    # order_by is needed again after annotate(), which drops the model's default ordering
    queryset = queryset.select_related("topic").annotate(example_count=Count("examples")).order_by("text")
    queryset, q, topic, difficulty = _search(queryset, request)
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))

    kept = request.GET.copy()
    kept.pop("page", None)
    context = {
        "heading": heading,
        "intro": intro,
        "page_obj": page,
        "q": q,
        "selected_topic": topic,
        "selected_difficulty": difficulty,
        "topics": Topic.objects.all(),
        "difficulties": Word.DIFFICULTY,
        "query_string": kept.urlencode(),
        "done": _done_message(request),
    }
    return render(request, "library/word_list.html", context)


# ---------------------------------------------------------------- read

@require_GET
def word_list(request):
    return _listing(request, Word.objects.all(), "Word library", "Search the words, or filter them by topic and difficulty.")


@require_GET
def words_by_difficulty(request, level):
    """/library/difficulty/<str:level>/ : easy, medium or hard."""
    if level.upper() not in DIFFICULTIES:
        raise Http404("Unknown difficulty level")
    queryset = Word.objects.filter(difficulty=level.upper())
    return _listing(request, queryset, f"{level.capitalize()} words")


@require_GET
def words_by_year(request, year):
    """/library/added/<year>/ : matched by a regular expression (re_path)."""
    queryset = Word.objects.filter(created_at__year=year)
    return _listing(request, queryset, f"Words added in {year}")


@require_GET
def word_by_id(request, pk):
    """/library/id/<int:pk>/ : send an old numeric link to the readable one."""
    word = get_object_or_404(Word, pk=pk)
    return redirect(word, permanent=True)


@require_GET
def word_detail(request, slug):
    word = get_object_or_404(Word.objects.select_related("topic").prefetch_related("examples"), slug=slug)
    context = {
        "word": word,
        "example_form": ExampleForm(),
        "related": Word.objects.filter(topic=word.topic).exclude(pk=word.pk)[:5],
        "done": _done_message(request),
    }
    return render(request, "library/word_detail.html", context)


@require_GET
def topic_list(request):
    topics = Topic.objects.annotate(word_count=Count("words")).order_by("name")
    return render(request, "library/topic_list.html", {"topics": topics})


@require_GET
def topic_detail(request, slug):
    topic = get_object_or_404(Topic, slug=slug)
    return _listing(request, Word.objects.filter(topic=topic), topic.name, topic.description)


# ------------------------------------------------- create, update, delete

@require_http_methods(["GET", "POST"])
def word_create(request):
    form = WordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        word = form.save()
        return redirect(f"{word.get_absolute_url()}?done=created")
    return render(request, "library/word_form.html", {"form": form, "title": "Add a word", "button": "Add word"})


@require_http_methods(["GET", "POST"])
def word_update(request, slug):
    word = get_object_or_404(Word, slug=slug)
    form = WordForm(request.POST or None, instance=word)
    if request.method == "POST" and form.is_valid():
        word = form.save()
        return redirect(f"{word.get_absolute_url()}?done=updated")
    context = {"form": form, "word": word, "title": f"Edit {word.text}", "button": "Save changes"}
    return render(request, "library/word_form.html", context)


@require_http_methods(["GET", "POST"])
def word_delete(request, slug):
    """GET asks "are you sure?", POST deletes the word and its examples."""
    word = get_object_or_404(Word, slug=slug)
    if request.method == "POST":
        word.delete()
        return redirect(f"{reverse('library:word_list')}?done=deleted")
    return render(request, "library/word_confirm_delete.html", {"word": word})


@require_POST
def example_add(request, slug):
    word = get_object_or_404(Word, slug=slug)
    form = ExampleForm(request.POST)
    if form.is_valid():
        form.instance.word = word
        form.save()
        return redirect(f"{word.get_absolute_url()}?done=example")
    context = {
        "word": word,
        "example_form": form,
        "related": Word.objects.filter(topic=word.topic).exclude(pk=word.pk)[:5],
    }
    return render(request, "library/word_detail.html", context, status=400)


@require_POST
def example_delete(request, pk):
    example = get_object_or_404(Example.objects.select_related("word"), pk=pk)
    word = example.word
    example.delete()
    return redirect(f"{word.get_absolute_url()}?done=example_deleted")


# ------------------------------------------------------------- JSON API

def _word_json(word, with_examples=False):
    data = {
        "text": word.text,
        "slug": word.slug,
        "topic": word.topic.name,
        "partOfSpeech": word.part_of_speech,
        "difficulty": word.difficulty,
        "definition": word.definition,
        "url": word.get_absolute_url(),
    }
    if with_examples:
        data["examples"] = [example.sentence for example in word.examples.all()]
    return data


@require_GET
def api_words(request):
    """GET /library/api/words/?q=&topic=&difficulty=&limit= -> {"count": n, "results": [...]}"""
    try:
        limit = int(request.GET.get("limit", 20))
    except ValueError:
        return JsonResponse({"error": "limit must be a whole number"}, status=400)
    limit = max(1, min(limit, 100))

    queryset, *_ = _search(Word.objects.select_related("topic"), request)
    words = list(queryset[:limit])
    return JsonResponse({"count": len(words), "results": [_word_json(word) for word in words]})


@require_GET
def api_word_detail(request, slug):
    """GET /library/api/words/<slug>/ -> one word with its examples, or a JSON 404."""
    word = Word.objects.select_related("topic").prefetch_related("examples").filter(slug=slug).first()
    if word is None:
        return JsonResponse({"error": "Word not found"}, status=404)
    return JsonResponse(_word_json(word, with_examples=True))


# --------------------------------------------------------- error pages

def page_not_found(request, exception=None):
    """Custom 404 page (used when DEBUG is False)."""
    return render(request, "404.html", status=404)


def server_error(request):
    """Custom 500 page. It does not use the base template, so it cannot fail itself."""
    return HttpResponseServerError(render_to_string("500.html"))
