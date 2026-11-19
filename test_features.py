"""Tests for topics, word-list rounds, the word of the day and the newer question types.

Uses the helpers of test_quiz_flow (in-memory MongoDB, fake Gemini). No key or network needed:

    python -m unittest test_features -v
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import mongomock
from bson import ObjectId
from django.core import signing
from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings

import test_quiz_flow as base
from services import gemini_service, mongo
from services import question_bank as bank
from services import round_builder as rounds
from services import word_of_the_day as wotd
from test_quiz_flow import BaseCase, FakeGemini, make_doc, seed


def docs_of(db, questions):
    return [db.questions.find_one({"_id": ObjectId(q["id"])}) for q in questions]


class LegacyWordTests(BaseCase):
    def test_first_version_word_still_works_when_gemini_is_down(self):
        old = [make_doc("alpha", t, version=1) for t in ("SYNONYM", "ANTONYM", "FILL_BLANK")]
        bank.save_questions(self.db, old)
        self.gemini.fail_with = gemini_service.GeminiAPIError("down", status_code=503, retryable=True)
        found = bank.ensure_word_questions(self.db, "alpha", "EASY")  # no exception
        self.assertEqual(len(found), 3)
        self.assertTrue(bank.acquire_lock(self.db, "word:alpha"))  # lock released

    def test_word_with_nothing_stored_still_fails(self):
        self.gemini.fail_with = gemini_service.GeminiAPIError("down", status_code=503, retryable=True)
        with self.assertRaises(gemini_service.GeminiAPIError):
            bank.ensure_word_questions(self.db, "alpha", "EASY")


class TopicRoundTests(BaseCase):
    def start(self, **kwargs):
        args = dict(word=None, difficulty="EASY", seen_ids=[], wrong_ids=[])
        args.update(kwargs)
        return rounds.build_round(self.db, **args)

    def test_topic_round_uses_only_that_topic(self):
        seed(self.db, words=6, topic="business")
        bank.save_questions(self.db, [make_doc(w, t, topic="travel")
                                      for w in base.WORDS[6:12] for t in gemini_service.SUPPORTED_TYPES])
        for _ in range(10):
            docs = docs_of(self.db, self.start(topic="business")["questions"])
            self.assertEqual({d["topic"] for d in docs}, {"business"})
        self.assertEqual(self.gemini.generate_calls, [])

    def test_empty_topic_is_refilled_for_that_topic(self):
        data = self.start(topic="science")
        self.assertEqual(len(data["questions"]), 5)
        self.assertEqual(self.gemini.suggest_topics, ["science"])
        self.assertEqual({d["topic"] for d in docs_of(self.db, data["questions"])}, {"science"})

    def test_topic_round_with_gemini_down_explains_itself(self):
        self.gemini.fail_with = gemini_service.GeminiAPIError("down", status_code=503, retryable=True)
        with self.assertRaises(rounds.NotEnoughQuestions) as ctx:
            self.start(topic="travel")
        self.assertIn("topic", ctx.exception.user_message)

    def test_unknown_topic_is_rejected(self):
        with self.assertRaises(ValueError):
            self.start(topic="cooking")
        self.assertEqual(self.gemini.generate_calls, [])

    def test_any_topic_values(self):
        seed(self.db)
        for value in (None, "", "any", "general"):
            self.assertEqual(len(self.start(topic=value)["questions"]), 5)

    def test_typed_word_comes_first_then_topic_questions(self):
        seed(self.db, words=6, topic="business")
        bank.save_questions(self.db, [make_doc("zulu", t, topic="travel") for t in gemini_service.SUPPORTED_TYPES])
        docs = docs_of(self.db, self.start(word="zulu", topic="business")["questions"])
        self.assertEqual([d["normalizedWord"] for d in docs[:3]], ["zulu"] * 3)
        self.assertEqual({d.get("topic") for d in docs[3:]}, {"business"})


class WordListRoundTests(BaseCase):
    def start(self, words, **kwargs):
        args = dict(word=None, difficulty="EASY", seen_ids=[], wrong_ids=[], words=words)
        args.update(kwargs)
        return rounds.build_round(self.db, **args)

    def test_round_uses_only_the_list_words(self):
        seed(self.db)
        for _ in range(10):
            data = self.start(["alpha", "bravo"])
            docs = docs_of(self.db, data["questions"])
            self.assertEqual(len(docs), 5)
            self.assertLessEqual({d["normalizedWord"] for d in docs}, {"alpha", "bravo"})
            self.assertEqual(len({d["_id"] for d in docs}), 5)
        self.assertEqual(self.gemini.generate_calls, [])

    def test_list_rounds_mix_question_types_and_words(self):
        seed(self.db)
        docs = docs_of(self.db, self.start(["alpha", "bravo", "cedar", "delta", "ember"])["questions"])
        self.assertGreaterEqual(len({d["normalizedWord"] for d in docs}), 4)
        self.assertGreaterEqual(len({d["questionType"] for d in docs}), 4)

    def test_missing_list_words_are_generated_a_couple_at_a_time(self):
        data = self.start(["zulu", "yankee", "xray"])
        self.assertEqual(self.gemini.generate_calls, [["zulu", "yankee"]])  # xray waits (no background in tests)
        self.assertEqual(len(data["questions"]), 5)
        words = {d["normalizedWord"] for d in docs_of(self.db, data["questions"])}
        self.assertLessEqual(words, {"zulu", "yankee"})

    def test_known_list_words_never_reach_gemini(self):
        seed(self.db, words=3)
        self.start(["alpha", "bravo", "cedar"])
        self.assertEqual(self.gemini.generate_calls, [])

    def test_invalid_entries_are_skipped_with_a_notice(self):
        seed(self.db)
        data = self.start(["alpha", "not a word", "123", "Bravo", "alpha"])
        self.assertIn("Skipped", data["notice"])
        self.assertIn("not a word", data["notice"])
        self.assertLessEqual({d["normalizedWord"] for d in docs_of(self.db, data["questions"])}, {"alpha", "bravo"})

    def test_list_with_no_valid_word_is_rejected(self):
        for words in (["123", "x y"], "alpha", {"a": 1}):
            with self.assertRaises(ValueError, msg=str(words)):
                self.start(words)

    def test_long_lists_are_capped(self):
        seed(self.db, words=12)
        names = [f"word{chr(97 + i)}" for i in range(26)]  # invalid: digits not allowed? letters only -> valid
        data = self.start(base.WORDS[:12] + names)
        self.assertEqual(len(data["questions"]), 5)

    def test_seen_questions_may_repeat_for_a_small_list(self):
        seed(self.db, words=1)
        ids = [str(d["_id"]) for d in self.db.questions.find({})]
        data = self.start(["alpha"], seen_ids=ids)
        self.assertEqual(len(data["questions"]), 5)
        self.assertIn("some may repeat", data["notice"])

    def test_words_that_cannot_be_created_fail_clearly(self):
        self.gemini.fail_with = gemini_service.GeminiInvalidResponse("nothing valid")
        with self.assertRaises(rounds.NotEnoughQuestions) as ctx:
            self.start(["qwertyuiop"])
        self.assertIn("could not create", ctx.exception.user_message)
        # the failed word is not sent to Gemini again straight away
        self.gemini.generate_calls.clear()
        with self.assertRaises(rounds.NotEnoughQuestions):
            self.start(["qwertyuiop"])
        self.assertEqual(self.gemini.generate_calls, [])

    def setUp(self):
        super().setUp()
        rounds._failed_words.clear()

    def test_wrong_questions_still_return_in_list_rounds(self):
        seed(self.db)
        ids = [str(d["_id"]) for d in self.db.questions.find({"normalizedWord": "cedar"})][:2]
        questions = self.start(["alpha", "bravo"], wrong_ids=ids)["questions"]
        self.assertEqual([q["review"] for q in questions], [False, False, True, False, True])


class NewQuestionTypeTests(BaseCase):
    """Answers and explanations for meaning, odd-one-out, word-form and spelling questions."""

    def setUp(self):
        super().setUp()
        seed(self.db)
        self.docs = {d["questionType"]: d for d in self.db.questions.find({"normalizedWord": "alpha"})}
        self.shown = {t: d["distractors"][: rounds.SHOWN_DISTRACTORS.get(t, 1)] for t, d in self.docs.items()}
        self.ids = {t: str(d["_id"]) for t, d in self.docs.items()}
        self.token = signing.dumps({"q": list(self.ids.values()),
                                    "d": {self.ids[t]: self.shown[t] for t in self.docs}}, salt=rounds.TOKEN_SALT)

    def check(self, qtype, chosen):
        return rounds.check_answer(self.db, token=self.token, question_id=self.ids[qtype], chosen=chosen)

    def test_round_options_have_the_right_size(self):
        seed(self.db, words=12)
        sizes = {}
        for _ in range(30):
            data = rounds.build_round(self.db, word=None, difficulty="EASY", seen_ids=[], wrong_ids=[])
            for q in data["questions"]:
                sizes.setdefault(q["type"], set()).add(len(q["options"]))
                self.assertEqual(len(set(q["options"])), len(q["options"]))
        for qtype, expected in {"SYNONYM": 2, "ANTONYM": 2, "FILL_BLANK": 2, "MEANING": 2,
                                "ODD_ONE_OUT": 4, "WORD_FORM": 3, "SPELLING": 3}.items():
            self.assertEqual(sizes.get(qtype), {expected}, msg=qtype)

    def test_meaning_answer_is_the_definition(self):
        doc = self.docs["MEANING"]
        result = self.check("MEANING", f"  {doc['correctAnswer'].upper()}")
        self.assertTrue(result["correct"])
        wrong = self.check("MEANING", self.shown["MEANING"][0])
        self.assertFalse(wrong["correct"])
        explanation = wrong["explanation"]
        self.assertEqual(explanation["title"], "What does alpha mean?")
        self.assertEqual(explanation["sections"][0]["heading"], "How is alpha used?")
        self.assertTrue(explanation["sections"][1]["heading"].startswith("What does alphad"))

    def test_odd_one_out(self):
        doc = self.docs["ODD_ONE_OUT"]
        for shown in self.shown["ODD_ONE_OUT"]:  # every shown word is a valid choice
            self.assertFalse(self.check("ODD_ONE_OUT", shown)["correct"])
        result = self.check("ODD_ONE_OUT", doc["correctAnswer"])
        self.assertTrue(result["correct"])
        explanation = result["explanation"]
        self.assertTrue(explanation["title"].startswith("Odd one out: "))
        self.assertIn("the same idea", explanation["answerSentence"])
        self.assertEqual(len(explanation["sections"]), 1 + 3)

    def test_word_form_explanation_completes_the_sentence(self):
        result = self.check("WORD_FORM", self.docs["WORD_FORM"]["correctAnswer"])
        self.assertTrue(result["correct"])
        self.assertEqual(result["explanation"]["answerSentence"], "Her alphaion was clear to everyone near alpha.")
        self.assertEqual(len(result["explanation"]["sections"]), 1 + 2)

    def test_spelling_explanation(self):
        result = self.check("SPELLING", self.shown["SPELLING"][1])
        self.assertFalse(result["correct"])
        sections = result["explanation"]["sections"]
        self.assertEqual(sections[0]["heading"], "What does alpha mean?")
        self.assertTrue(all(s["heading"].startswith("Why is") for s in sections[1:]))
        self.assertTrue(all(s["word"] is None for s in sections[1:]))

    def test_sections_carry_the_word_to_pronounce(self):
        sections = self.check("SYNONYM", None)["explanation"]["sections"]
        self.assertEqual(sections[0]["word"], "alpha")
        self.assertEqual(sections[1]["word"], "alphax")

    def test_a_wrong_option_that_was_not_shown_is_refused(self):
        hidden = self.docs["ODD_ONE_OUT"]["distractors"]
        self.shown["ODD_ONE_OUT"] = hidden[:2]
        self.token = signing.dumps({"q": list(self.ids.values()),
                                    "d": {self.ids[t]: self.shown[t] for t in self.docs}}, salt=rounds.TOKEN_SALT)
        with self.assertRaises(rounds.InvalidAnswer):
            self.check("ODD_ONE_OUT", hidden[2])


class WordOfTheDayTests(BaseCase):
    def setUp(self):
        super().setUp()
        wotd.clear_cache()
        self.addCleanup(wotd.clear_cache)

    def test_empty_bank_has_no_word(self):
        self.assertIsNone(wotd.word_of_the_day(self.db, date(2026, 10, 19)))

    def test_same_day_same_word_and_no_gemini_call(self):
        seed(self.db)
        first = wotd.word_of_the_day(self.db, date(2026, 10, 19))
        wotd.clear_cache()
        again = wotd.word_of_the_day(self.db, date(2026, 10, 19))
        self.assertEqual(first, again)
        self.assertEqual(self.gemini.generate_calls, [])
        self.assertEqual(self.gemini.suggest_calls, 0)

    def test_result_fields(self):
        seed(self.db)
        result = wotd.word_of_the_day(self.db, date(2026, 10, 19))
        self.assertEqual(set(result), {"word", "definition", "example", "difficulty", "date"})
        self.assertEqual(result["date"], "2026-10-19")
        self.assertEqual(result["definition"], f"meaning of {result['word']}")
        self.assertIn(result["word"], base.WORDS)

    def test_different_days_give_different_words(self):
        seed(self.db)
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(12)]
        words = {wotd.word_of_the_day(self.db, d)["word"] for d in days}
        self.assertGreater(len(words), 3)

    def test_words_added_today_do_not_change_todays_word(self):
        seed(self.db, words=1)
        self.db.questions.update_many({}, {"$set": {"createdAt": datetime(2020, 1, 1, tzinfo=timezone.utc)}})
        today = date(2026, 10, 19)
        before = wotd.word_of_the_day(self.db, today)
        wotd.clear_cache()
        bank.save_questions(self.db, [make_doc(w, "SYNONYM") for w in base.WORDS[1:8]])  # stored "now"
        self.assertEqual(wotd.word_of_the_day(self.db, today), before)
        self.assertEqual(before["word"], "alpha")

    def test_works_with_questions_of_every_type(self):
        for qtype in gemini_service.SUPPORTED_TYPES:
            self.db.questions.delete_many({})
            bank.save_questions(self.db, [make_doc("alpha", qtype)])
            wotd.clear_cache()
            self.assertEqual(wotd.word_of_the_day(self.db, date(2026, 10, 19))["word"], "alpha", msg=qtype)


@override_settings(ROOT_URLCONF="test_quiz_flow")
class FeatureViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        wotd.clear_cache()
        self.addCleanup(wotd.clear_cache)
        self.db = mongomock.MongoClient().wordwise_features
        mongo.ensure_indexes(self.db)
        seed(self.db, topic="business")
        self.gemini = FakeGemini()
        for patcher in (
            mock.patch("quiz.views.get_db", return_value=self.db),
            mock.patch.object(gemini_service, "generate_questions", side_effect=self.gemini.generate),
            mock.patch.object(gemini_service, "suggest_words", side_effect=self.gemini.suggest),
            mock.patch.object(rounds, "BACKGROUND_REFILL", False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        rounds._failed_words.clear()
        bank._budget.update({"day": None, "calls": 0})
        self.client = Client()

    def start(self, **payload):
        import json
        return self.client.post("/api/round/start", data=json.dumps({"difficulty": "EASY", **payload}),
                                content_type="application/json")

    def test_home_page_lists_the_topics(self):
        response = self.client.get("/")
        for label in ("Business and work", "Travel and places", "Science and nature"):
            self.assertContains(response, label)
        self.assertContains(response, 'id="topicSelect"')
        self.assertContains(response, "wordlist")

    def test_topic_round(self):
        data = self.start(topic="business").json()
        self.assertEqual(len(data["questions"]), 5)
        self.assertEqual({d["topic"] for d in docs_of(self.db, data["questions"])}, {"business"})

    def test_word_list_round(self):
        response = self.start(words=["alpha", "bravo", "not a word"])
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("Skipped", data["notice"])
        self.assertLessEqual({d["normalizedWord"] for d in docs_of(self.db, data["questions"])}, {"alpha", "bravo"})

    def test_bad_topic_and_word_lists(self):
        for payload in ({"topic": "cooking"}, {"topic": 5}, {"words": "alpha"}, {"words": [1, 2]},
                        {"words": ["a" + "b" * 50]}, {"words": ["alpha"] * 41}, {"words": ["123"]}):
            response = self.start(**payload)
            self.assertEqual(response.status_code, 400, msg=str(payload))
            self.assertIn("error", response.json())

    def test_word_of_the_day_endpoint(self):
        response = self.client.get("/api/word-of-the-day")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"word", "definition", "example", "difficulty", "date"})
        self.assertTrue(data["word"] and data["definition"] and data["example"])

    def test_word_of_the_day_uses_the_browsers_date_within_a_day(self):
        today = datetime.now(timezone.utc).date()
        near = self.client.get(f"/api/word-of-the-day?date={today.isoformat()}").json()
        self.assertEqual(near["date"], today.isoformat())
        far = self.client.get("/api/word-of-the-day?date=1999-01-01").json()
        self.assertEqual(far["date"], today.isoformat())  # too far away: server date is used
        junk = self.client.get("/api/word-of-the-day?date=junk").json()
        self.assertEqual(junk["date"], today.isoformat())

    def test_word_of_the_day_without_words_and_wrong_method(self):
        self.db.questions.delete_many({})
        self.assertEqual(self.client.get("/api/word-of-the-day").status_code, 404)
        self.assertEqual(self.client.post("/api/word-of-the-day").status_code, 405)

    def test_word_of_the_day_database_down(self):
        from pymongo.errors import ServerSelectionTimeoutError
        with mock.patch("quiz.views.get_db", side_effect=ServerSelectionTimeoutError("no server")):
            self.assertEqual(self.client.get("/api/word-of-the-day").status_code, 503)

    def test_word_of_the_day_is_rate_limited(self):
        with mock.patch("quiz.views.WOTD_LIMIT_PER_MINUTE", 2):
            codes = [self.client.get("/api/word-of-the-day").status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 429, 429])


if __name__ == "__main__":
    unittest.main()