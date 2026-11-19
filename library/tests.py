"""Tests for the word library.

Run them with:   python manage.py test library
"""

import json

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .models import Example, Topic, Word


def make_word(text="admire", topic=None, difficulty="EASY", definition="To respect someone."):
    topic = topic or Topic.objects.get_or_create(name="Feelings")[0]
    return Word.objects.create(text=text, topic=topic, difficulty=difficulty, definition=definition)


class ModelTests(TestCase):
    def test_slug_is_made_from_the_name(self):
        topic = Topic.objects.create(name="Science and Nature")
        self.assertEqual(topic.slug, "science-and-nature")

    def test_word_is_stored_in_lower_case_with_a_slug(self):
        word = make_word("  Admire ")
        self.assertEqual(word.text, "admire")
        self.assertEqual(word.slug, "admire")

    def test_duplicate_word_is_rejected_by_the_database(self):
        make_word("admire")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_word("admire")

    def test_difficulty_constraint_rejects_unknown_level(self):
        topic = Topic.objects.create(name="Travel")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Word.objects.create(text="trip", topic=topic, difficulty="IMPOSSIBLE", definition="A journey.")

    def test_deleting_a_word_deletes_its_examples(self):
        word = make_word()
        Example.objects.create(word=word, sentence="I admire her courage.")
        word.delete()
        self.assertEqual(Example.objects.count(), 0)

    def test_topic_with_words_cannot_be_deleted(self):
        word = make_word()
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            word.topic.delete()


class ReadPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.feelings = Topic.objects.create(name="Feelings")
        cls.business = Topic.objects.create(name="Business")
        cls.admire = make_word("admire", cls.feelings, "EASY", "To respect and like someone.")
        cls.negotiate = make_word("negotiate", cls.business, "HARD", "To discuss to reach an agreement.")
        Example.objects.create(word=cls.admire, sentence="I admire her courage.")

    def test_list_page_shows_every_word(self):
        response = self.client.get(reverse("library:word_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Admire")
        self.assertContains(response, "Negotiate")
        self.assertTemplateUsed(response, "base.html")

    def test_search_finds_a_word_by_its_text_or_meaning(self):
        by_text = self.client.get(reverse("library:word_list"), {"q": "negot"})
        self.assertContains(by_text, "Negotiate")
        self.assertNotContains(by_text, "Admire")
        by_meaning = self.client.get(reverse("library:word_list"), {"q": "respect"})
        self.assertContains(by_meaning, "Admire")

    def test_filter_by_topic_and_by_difficulty(self):
        topic = self.client.get(reverse("library:word_list"), {"topic": "business"})
        self.assertContains(topic, "Negotiate")
        self.assertNotContains(topic, "Admire")
        level = self.client.get(reverse("library:word_list"), {"difficulty": "easy"})
        self.assertContains(level, "Admire")
        self.assertNotContains(level, "Negotiate")

    def test_search_with_no_match_shows_the_empty_message(self):
        response = self.client.get(reverse("library:word_list"), {"q": "zzzz"})
        self.assertContains(response, "No words found")

    def test_detail_page_shows_meaning_and_examples(self):
        response = self.client.get(reverse("library:word_detail", args=["admire"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "To respect and like someone.")
        self.assertContains(response, "I admire her courage.")

    def test_unknown_word_gives_the_custom_404_page(self):
        response = self.client.get(reverse("library:word_detail", args=["nothing-here"]))
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "We could not find that page", status_code=404)

    def test_int_converter_redirects_to_the_slug_page(self):
        response = self.client.get(reverse("library:word_by_id", args=[self.admire.pk]))
        self.assertRedirects(response, self.admire.get_absolute_url(), status_code=301)

    def test_str_converter_page_and_its_404(self):
        ok = self.client.get(reverse("library:words_by_difficulty", args=["hard"]))
        self.assertContains(ok, "Negotiate")
        self.assertNotContains(ok, "Admire")
        self.assertEqual(self.client.get("/library/difficulty/legendary/").status_code, 404)

    def test_re_path_year_page(self):
        year = self.admire.created_at.year
        response = self.client.get(reverse("library:words_by_year", kwargs={"year": year}))
        self.assertContains(response, "Admire")
        self.assertEqual(self.client.get("/library/added/20x6/").status_code, 404)

    def test_topic_pages(self):
        listing = self.client.get(reverse("library:topic_list"))
        self.assertContains(listing, "Feelings")
        detail = self.client.get(reverse("library:topic_detail", args=["feelings"]))
        self.assertContains(detail, "Admire")
        self.assertNotContains(detail, "Negotiate")


class CrudTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.topic = Topic.objects.create(name="Travel")

    def test_create_a_word_with_post(self):
        response = self.client.post(reverse("library:word_create"), {
            "text": "Itinerary", "topic": self.topic.pk, "part_of_speech": "noun",
            "difficulty": "MEDIUM", "definition": "A plan of a journey.",
        })
        word = Word.objects.get(text="itinerary")
        self.assertRedirects(response, f"{word.get_absolute_url()}?done=created")
        self.assertEqual(word.topic, self.topic)

    def test_create_rejects_a_bad_word_and_a_duplicate(self):
        data = {"text": "it's 42", "topic": self.topic.pk, "part_of_speech": "noun",
                "difficulty": "EASY", "definition": "x"}
        bad = self.client.post(reverse("library:word_create"), data)
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(Word.objects.count(), 0)

        make_word("trip", self.topic)
        data.update(text="TRIP")
        duplicate = self.client.post(reverse("library:word_create"), data)
        self.assertContains(duplicate, "already in the library")
        self.assertEqual(Word.objects.count(), 1)

    def test_update_a_word(self):
        word = make_word("trip", self.topic, definition="old")
        response = self.client.post(reverse("library:word_update", args=[word.slug]), {
            "text": "trip", "topic": self.topic.pk, "part_of_speech": "noun",
            "difficulty": "HARD", "definition": "A short journey.",
        })
        self.assertEqual(response.status_code, 302)
        word.refresh_from_db()
        self.assertEqual(word.definition, "A short journey.")
        self.assertEqual(word.difficulty, "HARD")

    def test_delete_asks_first_then_deletes_on_post(self):
        word = make_word("trip", self.topic)
        ask = self.client.get(reverse("library:word_delete", args=[word.slug]))
        self.assertContains(ask, "Delete")
        self.assertTrue(Word.objects.filter(pk=word.pk).exists())

        self.client.post(reverse("library:word_delete", args=[word.slug]))
        self.assertFalse(Word.objects.filter(pk=word.pk).exists())

    def test_add_and_remove_an_example(self):
        word = make_word("trip", self.topic)
        self.client.post(reverse("library:example_add", args=[word.slug]), {"sentence": "We took a trip to Goa."})
        example = word.examples.get()
        self.assertEqual(example.sentence, "We took a trip to Goa.")

        short = self.client.post(reverse("library:example_add", args=[word.slug]), {"sentence": "no"})
        self.assertEqual(short.status_code, 400)

        self.client.post(reverse("library:example_delete", args=[example.pk]))
        self.assertEqual(word.examples.count(), 0)

    def test_deleting_by_get_is_not_allowed_for_examples(self):
        word = make_word("trip", self.topic)
        example = Example.objects.create(word=word, sentence="We took a trip to Goa.")
        response = self.client.get(reverse("library:example_delete", args=[example.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(Example.objects.filter(pk=example.pk).exists())


class JsonApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        topic = Topic.objects.create(name="Business")
        cls.word = make_word("negotiate", topic, "MEDIUM", "To reach an agreement.")
        make_word("revenue", topic, "EASY", "Money a company receives.")
        Example.objects.create(word=cls.word, sentence="They will negotiate the price.")

    def test_list_returns_json_and_can_search(self):
        response = self.client.get(reverse("library:api_words"), {"q": "negot"})
        self.assertEqual(response["Content-Type"], "application/json")
        data = json.loads(response.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["text"], "negotiate")

    def test_bad_limit_gives_a_json_400(self):
        response = self.client.get(reverse("library:api_words"), {"limit": "many"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", json.loads(response.content))

    def test_detail_returns_examples_and_unknown_word_is_a_json_404(self):
        ok = json.loads(self.client.get(reverse("library:api_word_detail", args=["negotiate"])).content)
        self.assertEqual(ok["examples"], ["They will negotiate the price."])
        missing = self.client.get(reverse("library:api_word_detail", args=["nope"]))
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(json.loads(missing.content)["error"], "Word not found")


class ErrorPageTests(TestCase):
    def test_custom_404_extends_the_base_template(self):
        response = self.client.get("/no/such/page/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertTemplateUsed(response, "base.html")

    def test_custom_500_page(self):
        from django.test import RequestFactory

        from .views import server_error

        response = server_error(RequestFactory().get("/"))
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"Something went wrong", response.content)


class AdminTests(TestCase):
    def test_models_are_registered_in_the_admin(self):
        from django.contrib import admin

        for model in (Topic, Word, Example):
            self.assertIn(model, admin.site._registry)
