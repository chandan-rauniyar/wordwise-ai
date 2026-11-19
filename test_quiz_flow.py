"""Tests for the question bank, round builder and Django API (Phases 4-6).

Needs no API key, network or MongoDB server: MongoDB is replaced by mongomock
and Gemini by a fake.

    pip install mongomock
    python -m unittest test_quiz_flow -v

(With manage.py use the module name, so your other test_*.py scripts are not run:
 python manage.py test test_quiz_flow)
"""

import json
import os
import threading
import time
import unittest
from unittest import mock

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test-secret-key-not-for-production",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["testserver"],
        INSTALLED_APPS=["quiz"],
        TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}],
        MIDDLEWARE=["django.middleware.csrf.CsrfViewMiddleware"],
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        STATIC_URL="/static/",
        USE_TZ=True,
    )
    django.setup()

import mongomock
from bson import ObjectId
from django.core import signing
from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings
from django.urls import include, path
from pymongo.errors import ServerSelectionTimeoutError

from services import gemini_service, mongo
from services import question_bank as bank
from services import round_builder as rounds
from services.gemini_service import GenerationResult

urlpatterns = [path("", include("quiz.urls"))]  # used as ROOT_URLCONF for the tests


# --------------------------------------------------------------------------
# Test data and fakes
# --------------------------------------------------------------------------

WORDS = ["alpha", "bravo", "cedar", "delta", "ember", "fable", "gamma", "harbor", "ivory", "jungle",
         "kettle", "lumen", "marble", "nectar", "orbit", "pebble", "quartz", "ripple", "saddle", "tundra"]


def info(word):
    return {"word": word, "definition": f"meaning of {word}", "example": f"An example with {word}."}


def make_doc(word, qtype, difficulty="EASY", version=2, topic=None):
    """A valid stored question for every supported type (what gemini_service would produce)."""
    correct, relation, answer, text = f"{word}x", "different", None, None
    distractors = [f"{word}d{i}" for i in (1, 2, 3)]
    extra = {}
    if qtype == "SYNONYM":
        relation, text = "similar", f"Which word is similar to {word}?"
    elif qtype == "ANTONYM":
        text = f"Which word is the opposite of {word}?"
    elif qtype == "FILL_BLANK":
        correct, relation = word, "fits"
        text = f"Please ___ it before {word} leaves the quiet room."
    elif qtype == "MEANING":
        correct, relation, text = word, "means", f"What does {word} mean?"
        answer = f"meaning of {word}"
    elif qtype == "ODD_ONE_OUT":
        correct, relation, text = f"{word}odd", "odd", "Which word does not belong with the others?"
        distractors = [word, f"{word}b", f"{word}c"]
        extra["theme"] = "the same idea"
    elif qtype == "WORD_FORM":
        correct, relation, text = f"{word}ion", "fits", f"Her ___ was clear to everyone near {word}."
        distractors = [f"{word}ed", f"{word}ing"]
    elif qtype == "SPELLING":
        correct, relation = word, "spelled"
        text = f"Which is the correct spelling of the word that means \u201cmeaning of {word}\u201d?"
        distractors = [f"{word}q{i}" for i in (1, 2)]
    option_texts = [f"meaning of {d}" for d in distractors] if qtype == "MEANING" else distractors
    sentence = text if qtype in gemini_service.SEPARATE_SENTENCE_TYPES else ""
    doc = {
        "word": word, "normalizedWord": word, "questionType": qtype, "difficulty": difficulty,
        "questionText": text, "correctAnswer": answer or correct, "distractors": option_texts,
        "explanation": {
            "target": info(word),
            "correct": {**info(correct if qtype != "MEANING" else word), "relation": relation},
            "distractors": [info(d) for d in distractors],
            **extra,
        },
        "uniqueKey": gemini_service.make_unique_key(word, qtype, correct, sentence),
        "source": "GEMINI", "model": "fake", "contentVersion": version,
    }
    if topic:
        doc["topic"] = topic
    return doc


class FakeGemini:
    """Stands in for gemini_service.generate_questions / suggest_words."""

    def __init__(self, delay=0.0):
        self.generate_calls = []
        self.generate_types = []
        self.suggest_calls = 0
        self.suggest_topics = []
        self.delay = delay
        self._next_word = 0
        self.fail_with = None

    def generate(self, words, difficulty="EASY", question_types=gemini_service.SUPPORTED_TYPES, **kwargs):
        self.generate_calls.append(list(words))
        self.generate_types.append(tuple(question_types))
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with:
            raise self.fail_with
        return GenerationResult(
            questions=[make_doc(w, t, difficulty) for w in words for t in question_types], model="fake")

    def suggest(self, difficulty, count=5, exclude=None, topic=None, **kwargs):
        self.suggest_calls += 1
        self.suggest_topics.append(topic)
        if self.fail_with:
            raise self.fail_with
        out = [WORDS[(self._next_word + i) % len(WORDS)] for i in range(count)]
        self._next_word += count
        return out


def seed(db, words=12, difficulty="EASY", topic=None, types=gemini_service.SUPPORTED_TYPES):
    docs = [make_doc(w, t, difficulty, topic=topic) for w in WORDS[:words] for t in types]
    bank.save_questions(db, docs)
    return docs


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().wordwise_test
        mongo.ensure_indexes(self.db)
        self.gemini = FakeGemini()
        for target, fake in (("generate_questions", self.gemini.generate), ("suggest_words", self.gemini.suggest)):
            patcher = mock.patch.object(gemini_service, target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        bank._budget.update({"day": None, "calls": 0})
        patcher = mock.patch.object(rounds, "BACKGROUND_REFILL", False)
        patcher.start()
        self.addCleanup(patcher.stop)


# --------------------------------------------------------------------------
# Phase 4: question bank
# --------------------------------------------------------------------------

class BankTests(BaseCase):
    def test_save_skips_duplicates(self):
        doc = make_doc("alpha", "SYNONYM")
        self.assertEqual(bank.save_questions(self.db, [doc]), (1, 0))
        self.assertEqual(bank.save_questions(self.db, [doc, make_doc("alpha", "ANTONYM")]), (1, 1))
        self.assertEqual(self.db.questions.count_documents({}), 2)

    def test_saved_documents_get_timestamps(self):
        bank.save_questions(self.db, [make_doc("alpha", "SYNONYM")])
        saved = self.db.questions.find_one({})
        self.assertIn("createdAt", saved)
        self.assertIn("updatedAt", saved)

    def test_to_object_ids_ignores_bad_and_duplicate_values(self):
        good = str(ObjectId())
        ids = bank.to_object_ids([good, good, "nope", 5, None, "z" * 24], limit=10)
        self.assertEqual(ids, [ObjectId(good)])
        self.assertEqual(len(bank.to_object_ids([str(ObjectId()) for _ in range(20)], limit=5)), 5)

    def test_unseen_helpers(self):
        docs = seed(self.db, words=4)
        all_ids = [d["_id"] for d in self.db.questions.find({})]
        self.assertEqual(bank.count_unseen(self.db, "EASY", all_ids[:5]), len(docs) - 5)
        picked = bank.find_unseen(self.db, "EASY", all_ids[:5], limit=100)
        self.assertEqual(len(picked), len(docs) - 5)
        self.assertTrue(all(d["_id"] not in all_ids[:5] for d in picked))
        self.assertEqual(bank.find_unseen(self.db, "HARD", [], limit=5), [])

    def test_get_many_keeps_order(self):
        seed(self.db, words=2)
        ids = [d["_id"] for d in self.db.questions.find({})][::-1]
        self.assertEqual([d["_id"] for d in bank.get_many(self.db, ids)], ids)

    def test_word_is_generated_once_then_reused(self):
        first = bank.ensure_word_questions(self.db, "Alpha", "EASY")
        self.assertEqual(len(first), 7)  # one question per type
        self.assertEqual(self.gemini.generate_calls, [["alpha"]])
        second = bank.ensure_word_questions(self.db, "alpha", "MEDIUM")
        self.assertEqual(len(second), 7)
        self.assertEqual(len(self.gemini.generate_calls), 1)  # no second Gemini call

    def test_existing_word_does_not_call_gemini(self):
        seed(self.db, words=1)
        bank.ensure_word_questions(self.db, "alpha", "EASY")
        self.assertEqual(self.gemini.generate_calls, [])

    def test_first_version_word_only_gets_the_new_question_types(self):
        old = [make_doc("alpha", t, version=1) for t in ("SYNONYM", "ANTONYM", "FILL_BLANK")]
        bank.save_questions(self.db, old)
        result = bank.ensure_word_questions(self.db, "alpha", "EASY")
        self.assertEqual(self.gemini.generate_calls, [["alpha"]])
        self.assertEqual(self.gemini.generate_types, [("MEANING", "ODD_ONE_OUT", "WORD_FORM", "SPELLING")])
        self.assertEqual(len(result), 7)
        # now it is complete: asking again does not call Gemini
        bank.ensure_word_questions(self.db, "alpha", "EASY")
        self.assertEqual(len(self.gemini.generate_calls), 1)

    def test_several_words_are_generated_together_and_complete_ones_skipped(self):
        seed(self.db, words=1)  # alpha is complete
        found = bank.ensure_words_questions(self.db, ["alpha", "Bravo", "cedar", "bravo"], "EASY")
        self.assertEqual(self.gemini.generate_calls, [["bravo", "cedar"]])
        self.assertEqual({w: len(d) for w, d in found.items()}, {"alpha": 7, "bravo": 7, "cedar": 7})
        self.assertEqual(bank.incomplete_words(self.db, ["alpha", "zulu"]), ["zulu"])

    def test_non_strict_generation_logs_and_continues(self):
        self.gemini.fail_with = gemini_service.GeminiInvalidResponse("nothing valid")
        found = bank.ensure_words_questions(self.db, ["alpha"], "EASY", strict=False)
        self.assertEqual(found, {"alpha": []})
        self.assertTrue(bank.acquire_lock(self.db, "word:alpha"))  # lock was released

    def test_generation_failure_saves_nothing_and_releases_lock(self):
        self.gemini.fail_with = gemini_service.GeminiInvalidResponse("bad")
        with self.assertRaises(gemini_service.GeminiInvalidResponse):
            bank.ensure_word_questions(self.db, "alpha", "EASY")
        self.assertEqual(self.db.questions.count_documents({}), 0)
        self.assertTrue(bank.acquire_lock(self.db, "word:alpha"))

    def test_invalid_word_never_reaches_gemini(self):
        with self.assertRaises(ValueError):
            bank.ensure_word_questions(self.db, "two words", "EASY")
        self.assertEqual(self.gemini.generate_calls, [])

    def test_lock_behaviour(self):
        self.assertTrue(bank.acquire_lock(self.db, "k"))
        self.assertFalse(bank.acquire_lock(self.db, "k"))
        bank.release_lock(self.db, "k")
        self.assertTrue(bank.acquire_lock(self.db, "k"))
        # an expired lock (TTL monitor can lag) is replaced
        bank.release_lock(self.db, "k")
        self.assertTrue(bank.acquire_lock(self.db, "k", ttl_seconds=-5))
        self.assertTrue(bank.acquire_lock(self.db, "k"))

    def test_two_requests_for_the_same_word_generate_once(self):
        self.gemini.delay = 0.4
        results = []
        threads = [threading.Thread(target=lambda: results.append(len(bank.ensure_word_questions(self.db, "sigma", "EASY"))))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(results, [7, 7])
        self.assertEqual(len(self.gemini.generate_calls), 1)

    def test_refill_adds_new_words_only(self):
        seed(self.db, words=2)  # alpha, bravo already known
        saved = bank.refill(self.db, "EASY", words=5)  # fake suggests alpha..ember
        self.assertEqual(saved, 21)  # cedar, delta, ember x 7 types
        self.assertEqual(self.gemini.generate_calls, [["cedar", "delta", "ember"]])
        self.assertEqual(self.gemini.suggest_topics, [None])

    def test_refill_for_a_topic_tags_the_questions(self):
        saved = bank.refill(self.db, "EASY", topic="business", words=2)
        self.assertEqual(saved, 14)
        self.assertEqual(self.gemini.suggest_topics, ["business"])
        self.assertEqual(self.db.questions.count_documents({"topic": "business"}), 14)
        self.assertEqual(bank.count_unseen(self.db, "EASY", [], topic="business"), 14)
        self.assertEqual(bank.count_unseen(self.db, "EASY", [], topic="travel"), 0)
        self.assertEqual(len(bank.find_unseen(self.db, "EASY", [], 50, topic="business")), 14)
        with self.assertRaises(ValueError):
            bank.refill(self.db, "EASY", topic="cooking")

    def test_refill_is_skipped_while_another_refill_runs(self):
        bank.acquire_lock(self.db, "refill:EASY:any")
        self.assertEqual(bank.refill(self.db, "EASY"), 0)
        self.assertEqual(self.gemini.suggest_calls, 0)
        self.assertEqual(bank.refill(self.db, "EASY", topic="travel", words=1), 7)  # other topic: own lock

    def test_daily_limit_stops_gemini_calls(self):
        with mock.patch.dict(os.environ, {"GEMINI_DAILY_CALL_LIMIT": "1"}):
            bank.ensure_word_questions(self.db, "alpha", "EASY")
            with self.assertRaises(bank.DailyLimitReached):
                bank.ensure_word_questions(self.db, "bravo", "EASY")
        self.assertEqual(len(self.gemini.generate_calls), 1)


# --------------------------------------------------------------------------
# Phase 5: round builder and answer checking
# --------------------------------------------------------------------------

PUBLIC_KEYS = {"id", "type", "text", "options", "review"}


class RoundTests(BaseCase):
    def start(self, **kwargs):
        args = dict(word=None, difficulty="EASY", seen_ids=[], wrong_ids=[])
        args.update(kwargs)
        return rounds.build_round(self.db, **args)

    def test_round_has_five_unique_questions_without_answers(self):
        seed(self.db)
        data = self.start()
        questions = data["questions"]
        self.assertEqual(len(questions), 5)
        self.assertEqual(len({q["id"] for q in questions}), 5)
        for q in questions:
            self.assertEqual(set(q), PUBLIC_KEYS)
            self.assertEqual(len(q["options"]), 1 + rounds.SHOWN_DISTRACTORS.get(q["type"], 1))
        self.assertNotIn("explanation", json.dumps(data))
        self.assertNotIn("distractors", json.dumps(data))
        self.assertIsNone(data["notice"])
        self.assertEqual(self.gemini.generate_calls, [])  # served from the database

    def test_options_contain_the_correct_answer_and_are_shuffled(self):
        seed(self.db)
        positions = set()
        for _ in range(30):
            for q in self.start()["questions"]:
                doc = self.db.questions.find_one({"_id": ObjectId(q["id"])})
                self.assertIn(doc["correctAnswer"], q["options"])
                positions.add(q["options"].index(doc["correctAnswer"]))
        self.assertGreaterEqual(len(positions), 3)  # the correct answer is not always in the same place

    def test_word_mode_puts_the_word_first(self):
        seed(self.db)
        questions = self.start(word="alpha")["questions"]
        self.assertEqual(len(questions), 5)
        first_three = [self.db.questions.find_one({"_id": ObjectId(q["id"])}) for q in questions[:3]]
        self.assertEqual({d["normalizedWord"] for d in first_three}, {"alpha"})
        types = [d["questionType"] for d in first_three]
        self.assertEqual(len(set(types)), 3)  # three different question types
        self.assertEqual(types, sorted(types, key=rounds.TYPE_ORDER.get))
        later = [self.db.questions.find_one({"_id": ObjectId(q["id"])})["normalizedWord"] for q in questions[3:]]
        self.assertNotIn("alpha", later)

    def test_new_word_is_generated_when_missing(self):
        seed(self.db)
        questions = self.start(word="zephyr")["questions"]
        self.assertEqual(self.gemini.generate_calls, [["zephyr"]])
        first_three = [self.db.questions.find_one({"_id": ObjectId(q["id"])}) for q in questions[:3]]
        self.assertEqual({d["normalizedWord"] for d in first_three}, {"zephyr"})

    def test_seen_questions_are_not_repeated(self):
        seed(self.db, words=4)  # 28 questions
        ids = [str(d["_id"]) for d in self.db.questions.find({})]
        seen, unseen = ids[:23], set(ids[23:])  # exactly 5 left
        questions = self.start(seen_ids=seen)["questions"]
        self.assertEqual({q["id"] for q in questions}, unseen)
        self.assertEqual(len(self.gemini.generate_calls), 0)

    def test_rounds_mix_words_and_question_types(self):
        seed(self.db)
        for _ in range(15):
            docs = [self.db.questions.find_one({"_id": ObjectId(q["id"])}) for q in self.start()["questions"]]
            self.assertEqual(len({d["normalizedWord"] for d in docs}), 5)
            self.assertGreaterEqual(len({d["questionType"] for d in docs}), 4)

    def test_wrong_questions_return_after_other_questions(self):
        seed(self.db)
        ids = [str(d["_id"]) for d in self.db.questions.find({})]
        wrong = ids[:3]
        for _ in range(20):
            questions = self.start(wrong_ids=wrong)["questions"]
            self.assertEqual([q["review"] for q in questions], [False, False, True, False, True])
            self.assertTrue(all(q["id"] in wrong for q in questions if q["review"]))
            self.assertEqual(len({q["id"] for q in questions}), 5)

    def test_review_never_comes_first_even_in_a_tiny_round(self):
        seed(self.db, words=1, types=("SYNONYM", "ANTONYM", "FILL_BLANK"))  # only 3 questions exist
        ids = [str(d["_id"]) for d in self.db.questions.find({})]
        questions = self.start(wrong_ids=ids[:1], seen_ids=ids)["questions"]
        self.assertFalse(questions[0]["review"])

    def test_small_bank_falls_back_to_seen_questions(self):
        seed(self.db, words=1)
        ids = [str(d["_id"]) for d in self.db.questions.find({})]
        self.gemini.fail_with = gemini_service.GeminiAPIError("down", status_code=503, retryable=True)
        data = self.start(seen_ids=ids)
        self.assertEqual(len(data["questions"]), 5)
        self.assertIn("some may repeat", data["notice"])

    def test_empty_bank_is_filled_from_gemini(self):
        data = self.start()
        self.assertEqual(len(data["questions"]), 5)
        self.assertEqual(self.gemini.suggest_calls, 1)

    def test_empty_bank_and_gemini_down_raises(self):
        self.gemini.fail_with = gemini_service.GeminiAPIError("down", status_code=503, retryable=True)
        with self.assertRaises(rounds.NotEnoughQuestions):
            self.start()

    def test_typed_word_failure_is_raised(self):
        self.gemini.fail_with = gemini_service.GeminiInvalidResponse("nothing valid")
        with self.assertRaises(gemini_service.GeminiInvalidResponse):
            self.start(word="zzzzzz")

    def test_bad_input(self):
        seed(self.db)
        with self.assertRaises(ValueError):
            self.start(difficulty="IMPOSSIBLE")
        with self.assertRaises(ValueError):
            self.start(word="not a word")
        self.assertEqual(len(self.start(word="   ")["questions"]), 5)  # blank word = random round

    def test_background_refill_only_when_stock_is_low(self):
        seed(self.db, words=5)  # 35 questions
        with mock.patch.object(rounds, "BACKGROUND_REFILL", True), \
                mock.patch.object(rounds, "_in_background") as background:
            self.start()
            background.assert_not_called()
            ids = [str(d["_id"]) for d in self.db.questions.find({})]
            self.start(seen_ids=ids[:20])  # 15 unseen, 10 after this round: below the watermark
            background.assert_called_once()
            self.assertEqual(background.call_args[0][0], "refill-EASY")


class AnswerTests(BaseCase):
    def setUp(self):
        super().setUp()
        seed(self.db)
        # a hand-made round with every question type of "alpha", so each explanation can be checked
        self.docs = {d["questionType"]: d for d in self.db.questions.find({"normalizedWord": "alpha"})}
        self.shown = {t: d["distractors"][: rounds.SHOWN_DISTRACTORS.get(t, 1)] for t, d in self.docs.items()}
        self.questions = {t: {"id": str(d["_id"]), "options": [d["correctAnswer"]] + self.shown[t]}
                          for t, d in self.docs.items()}
        self.token = signing.dumps(
            {"q": [q["id"] for q in self.questions.values()],
             "d": {self.questions[t]["id"]: self.shown[t] for t in self.docs}}, salt=rounds.TOKEN_SALT)
        self.q = self.questions["SYNONYM"]
        self.doc = self.docs["SYNONYM"]

    def check(self, chosen, question=None, token=None):
        return rounds.check_answer(self.db, token=token or self.token,
                                   question_id=(question or self.q)["id"], chosen=chosen)

    def test_correct_answer(self):
        result = self.check(self.doc["correctAnswer"])
        self.assertTrue(result["correct"])
        self.assertEqual(result["points"], 100)
        self.assertFalse(result["skipped"])

    def test_answer_is_case_and_space_insensitive(self):
        self.assertTrue(self.check(f"  {self.doc['correctAnswer'].upper()} ")["correct"])

    def test_wrong_answer_reveals_the_correct_one(self):
        wrong = next(o for o in self.q["options"] if o != self.doc["correctAnswer"])
        result = self.check(wrong)
        self.assertFalse(result["correct"])
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["correctAnswer"], self.doc["correctAnswer"])

    def test_skip(self):
        result = self.check(None)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["correct"])
        self.assertEqual(result["points"], 0)

    def test_answer_must_be_one_of_the_choices(self):
        with self.assertRaises(rounds.InvalidAnswer):
            self.check("banana")
        with self.assertRaises(rounds.InvalidAnswer):
            self.check(42)
        # a distractor that was not shown is not a valid answer either
        not_shown = next(d for d in self.doc["distractors"] if d not in self.q["options"])
        with self.assertRaises(rounds.InvalidAnswer):
            self.check(not_shown)

    def test_token_problems(self):
        with self.assertRaises(rounds.InvalidRound):
            self.check("x", token=self.token[:-3] + "abc")  # tampered
        with self.assertRaises(rounds.InvalidRound):
            self.check("x", token="garbage")
        other_id = str(next(d["_id"] for d in self.db.questions.find({"normalizedWord": "bravo"})))
        with self.assertRaises(rounds.InvalidRound):
            self.check("x", question={"id": other_id})  # not part of this round
        with mock.patch.object(rounds, "TOKEN_MAX_AGE_SECONDS", -1):
            with self.assertRaises(rounds.InvalidRound):
                self.check(self.doc["correctAnswer"])  # expired

    def test_explanation_for_similar_and_opposite_words(self):
        explanation = self.check(None)["explanation"]
        self.assertEqual(explanation["title"], "Word similar to alpha?")
        headings = [s["heading"] for s in explanation["sections"]]
        self.assertEqual(headings[0], "What's the definition of alpha?")
        self.assertEqual(headings[1], "How is alphax similar?")
        self.assertTrue(headings[2].startswith("What's the definition of alphad"))
        for section in explanation["sections"]:
            self.assertTrue(section["text"] and section["example"])
        self.assertIsNone(explanation["answerSentence"])

        explanation = self.check(None, question=self.questions["ANTONYM"])["explanation"]
        self.assertEqual(explanation["title"], "Word opposite of alpha?")
        self.assertEqual(explanation["sections"][1]["heading"], "How is alphax different?")

    def test_explanation_for_fill_in_the_blank(self):
        explanation = self.check(None, question=self.questions["FILL_BLANK"])["explanation"]
        self.assertEqual(explanation["answerSentence"], "Please alpha it before alpha leaves the quiet room.")
        self.assertEqual(explanation["sections"][0]["heading"], "What does alpha mean?")


# --------------------------------------------------------------------------
# Phase 6: Django views
# --------------------------------------------------------------------------

@override_settings(ROOT_URLCONF=__name__)
class ViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.db = mongomock.MongoClient().wordwise_views
        mongo.ensure_indexes(self.db)
        seed(self.db)
        self.gemini = FakeGemini()
        for name, patcher in (
            ("db", mock.patch("quiz.views.get_db", return_value=self.db)),
            ("gen", mock.patch.object(gemini_service, "generate_questions", side_effect=self.gemini.generate)),
            ("sug", mock.patch.object(gemini_service, "suggest_words", side_effect=self.gemini.suggest)),
            ("bg", mock.patch.object(rounds, "BACKGROUND_REFILL", False)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        bank._budget.update({"day": None, "calls": 0})
        self.client = Client()

    def post(self, url, payload, client=None, raw=None):
        body = raw if raw is not None else json.dumps(payload)
        return (client or self.client).post(url, data=body, content_type="application/json")

    def start(self, **payload):
        return self.post("/api/round/start", {"difficulty": "EASY", **payload})

    def test_home_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "WordWise")
        self.assertIn("csrftoken", response.cookies)

    def test_full_round_flow(self):
        response = self.start(word="alpha")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["questions"]), 5)
        self.assertNotIn("correctAnswer", response.content.decode())

        points = 0
        for i, q in enumerate(data["questions"]):
            correct = self.db.questions.find_one({"_id": ObjectId(q["id"])})["correctAnswer"]
            chosen = correct if i % 2 == 0 else None  # answer, skip, answer, skip, answer
            result = self.post("/api/answer", {"roundToken": data["roundToken"], "questionId": q["id"], "chosen": chosen})
            self.assertEqual(result.status_code, 200)
            body = result.json()
            self.assertEqual(body["correct"], chosen is not None)
            self.assertEqual(body["skipped"], chosen is None)
            self.assertEqual(body["correctAnswer"], correct)
            self.assertTrue(body["explanation"]["sections"])
            points += body["points"]
        self.assertEqual(points, 300)

    def test_wrong_questions_come_back_marked_for_review(self):
        first = self.start().json()
        wrong = [q["id"] for q in first["questions"][:3]]
        second = self.start(wrongIds=wrong).json()
        self.assertEqual(sum(q["review"] for q in second["questions"]), 2)

    def test_start_rejects_bad_requests(self):
        cases = [
            {"raw": "not json"},
            {"raw": "[1, 2]"},
            {"payload": {"difficulty": "EASY", "word": "two words"}},
            {"payload": {"difficulty": "EASY", "word": 5}},
            {"payload": {"difficulty": "IMPOSSIBLE"}},
            {"payload": {"difficulty": 7}},
            {"payload": {"difficulty": "EASY", "seenIds": "abc"}},
            {"payload": {"difficulty": "EASY", "wrongIds": [1, 2]}},
        ]
        for case in cases:
            response = self.post("/api/round/start", case.get("payload"), raw=case.get("raw"))
            self.assertEqual(response.status_code, 400, msg=str(case))
            self.assertIn("error", response.json())

    def test_oversized_body_is_rejected(self):
        response = self.post("/api/round/start", None, raw=json.dumps({"seenIds": ["a" * 24] * 5000}))
        self.assertEqual(response.status_code, 400)

    def test_only_post_is_allowed_on_the_api(self):
        self.assertEqual(self.client.get("/api/round/start").status_code, 405)
        self.assertEqual(self.client.get("/api/answer").status_code, 405)

    def test_answer_rejects_bad_requests(self):
        data = self.start().json()
        qid = data["questions"][0]["id"]
        bad = [
            {"questionId": qid},
            {"roundToken": data["roundToken"]},
            {"roundToken": "garbage", "questionId": qid, "chosen": None},
            {"roundToken": data["roundToken"], "questionId": qid, "chosen": "banana"},
            {"roundToken": data["roundToken"], "questionId": qid, "chosen": 5},
            {"roundToken": data["roundToken"], "questionId": str(ObjectId()), "chosen": None},
        ]
        for payload in bad:
            self.assertEqual(self.post("/api/answer", payload).status_code, 400, msg=str(payload))

    def test_unknown_word_gets_a_helpful_message(self):
        self.gemini.fail_with = gemini_service.GeminiInvalidResponse("nothing valid")
        response = self.start(word="qwertyuiop")
        self.assertEqual(response.status_code, 422)
        self.assertIn("spelling", response.json()["error"])

    def test_gemini_quota_and_outage(self):
        self.gemini.fail_with = gemini_service.GeminiAPIError("quota", status_code=429, retryable=True,
                                                              user_message="busy")
        response = self.start(word="newword")
        self.assertEqual((response.status_code, response.json()["error"]), (429, "busy"))
        self.gemini.fail_with = gemini_service.GeminiAPIError("down", status_code=503, retryable=True,
                                                              user_message="down")
        self.assertEqual(self.start(word="another").status_code, 503)

    def test_daily_limit_message(self):
        with mock.patch.dict(os.environ, {"GEMINI_DAILY_CALL_LIMIT": "0"}):
            response = self.start(word="newword")
        self.assertEqual(response.status_code, 503)
        self.assertIn("limit", response.json()["error"])

    def test_database_down(self):
        with mock.patch("quiz.views.get_db", side_effect=ServerSelectionTimeoutError("no server")):
            response = self.start()
        self.assertEqual(response.status_code, 503)
        self.assertIn("database", response.json()["error"])

    def test_rate_limit(self):
        with mock.patch("quiz.views.START_LIMIT_PER_MINUTE", 3):
            codes = [self.start().status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_csrf_is_enforced(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post("/api/round/start", {"difficulty": "EASY"}, client=strict).status_code, 403)

    def test_nothing_about_the_user_is_saved(self):
        before = {name: self.db[name].count_documents({}) for name in self.db.list_collection_names()}
        data = self.start(wrongIds=[str(ObjectId())], seenIds=[str(ObjectId())]).json()
        q = data["questions"][0]
        self.post("/api/answer", {"roundToken": data["roundToken"], "questionId": q["id"], "chosen": None})
        after = {name: self.db[name].count_documents({}) for name in self.db.list_collection_names()}
        self.assertEqual(before.get("questions"), after.get("questions"))
        self.assertEqual(set(after) - {"questions", "generation_locks"}, set())


if __name__ == "__main__":
    unittest.main()