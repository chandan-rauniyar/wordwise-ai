"""Offline tests for services/gemini_service.py (no API key or network needed).

Run from the project root:
    python -m unittest test_gemini_service -v
"""

import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from google.genai import errors as genai_errors

from services import gemini_service as gs


def info(word, definition="a short definition", example=None):
    return {"word": word, "definition": definition, "example": example or f"This is an example with {word}."}


def synonym_q(word="admire"):
    return {
        "target_word": word, "question_type": "SYNONYM", "sentence": "",
        "target": info(word), "correct": info("esteem"),
        "distractors": [info("enroll"), info("mention"), info("borrow")],
    }


def antonym_q(word="admire"):
    return {
        "target_word": word, "question_type": "ANTONYM", "sentence": "",
        "target": info(word), "correct": info("despise"),
        "distractors": [info("desire"), info("reveal"), info("gather")],
    }


def blank_q(word="admire", sentence="I really ___ her courage in hard times."):
    return {
        "target_word": word, "question_type": "FILL_BLANK", "sentence": sentence,
        "target": info(word), "correct": info(word),
        "distractors": [info("enroll"), info("borrow"), info("gather")],
    }


def payload(*questions):
    return json.dumps({"questions": list(questions)})


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.prompts = []

    def generate_content(self, **kwargs):
        self.calls += 1
        self.prompts.append(kwargs.get("contents", ""))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


def fake_client(*outcomes):
    client = SimpleNamespace(models=FakeModels(outcomes))
    return client


def api_error(code):
    return genai_errors.APIError(code, {"error": {"message": "x", "status": "X"}})


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        gs._sleep = lambda seconds: None  # no waiting during retry tests


class NormalizeTests(ServiceTestCase):
    def test_normalizes(self):
        self.assertEqual(gs.normalize_word("  Admire "), "admire")
        self.assertEqual(gs.normalize_word("Well-known"), "well-known")

    def test_rejects_bad_input(self):
        for bad in ["", "two words", "adm1re", "<script>", "a", "x" * 40, None, 5]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                gs.normalize_word(bad)


class ValidBatchTests(ServiceTestCase):
    def test_valid_batch_builds_documents(self):
        client = fake_client(payload(synonym_q(), antonym_q(), blank_q()))
        result = gs.generate_questions(["Admire"], "easy", ["SYNONYM", "ANTONYM", "FILL_BLANK"],
                                       client=client, model="m")
        self.assertEqual(len(result.questions), 3)
        self.assertEqual(result.rejected, [])
        self.assertEqual(result.missing, [])
        by_type = {q["questionType"]: q for q in result.questions}

        syn = by_type["SYNONYM"]
        self.assertEqual(syn["questionText"], "Which word is similar to admire?")
        self.assertEqual(syn["correctAnswer"], "esteem")
        self.assertEqual(len(syn["distractors"]), 3)
        self.assertEqual(syn["explanation"]["correct"]["relation"], "similar")
        self.assertEqual(syn["difficulty"], "EASY")
        self.assertEqual(syn["uniqueKey"], "admire:SYNONYM:esteem")
        self.assertEqual(syn["source"], "GEMINI")

        ant = by_type["ANTONYM"]
        self.assertEqual(ant["questionText"], "Which word is the opposite of admire?")
        self.assertEqual(ant["explanation"]["correct"]["relation"], "different")

        blank = by_type["FILL_BLANK"]
        self.assertEqual(blank["questionText"], "I really ___ her courage in hard times.")
        self.assertEqual(blank["correctAnswer"], "admire")
        self.assertTrue(blank["uniqueKey"].startswith("admire:FILL_BLANK:admire:"))

    def test_difficulty_comes_from_request_not_gemini(self):
        client = fake_client(payload(synonym_q()))
        result = gs.generate_questions(["admire"], "HARD", ["SYNONYM"], client=client, model="m")
        self.assertEqual(result.questions[0]["difficulty"], "HARD")

    def test_duplicate_in_batch_removed(self):
        client = fake_client(payload(synonym_q(), synonym_q()))
        result = gs.generate_questions(["admire"], "EASY", ["SYNONYM"], client=client, model="m")
        self.assertEqual(len(result.questions), 1)
        self.assertEqual(result.rejected[0]["reason"], "duplicate in batch")

    def test_missing_antonym_is_reported(self):
        client = fake_client(payload(synonym_q()))
        result = gs.generate_questions(["admire"], "EASY", ["SYNONYM", "ANTONYM"], client=client, model="m")
        self.assertEqual(result.missing, [("admire", "ANTONYM")])


class RejectionTests(ServiceTestCase):
    def run_one(self, bad, good=None):
        questions = [bad] + ([good] if good else [])
        client = fake_client(payload(*questions))
        return gs.generate_questions(["admire"], "EASY", client=client, model="m")

    def test_bad_question_dropped_good_one_kept(self):
        bad = synonym_q()
        bad["distractors"][0] = info("esteem")  # same as the correct answer
        result = self.run_one(bad, antonym_q())
        self.assertEqual([q["questionType"] for q in result.questions], ["ANTONYM"])
        self.assertIn("not distinct", result.rejected[0]["reason"])

    def test_too_few_distractors(self):
        bad = synonym_q()
        bad["distractors"] = bad["distractors"][:2]
        with self.assertRaises(gs.GeminiInvalidResponse) as ctx:
            self.run_one(bad)
        self.assertIn("distractors", ctx.exception.rejected[0]["reason"])

    def test_target_word_among_options(self):
        bad = antonym_q()
        bad["distractors"][1] = info("admire")
        with self.assertRaises(gs.GeminiInvalidResponse):
            self.run_one(bad)

    def test_unrequested_word(self):
        with self.assertRaises(gs.GeminiInvalidResponse):
            self.run_one(synonym_q("sphinx"))

    def test_unrequested_type(self):
        bad = synonym_q()
        bad["question_type"] = "TRIVIA"
        with self.assertRaises(gs.GeminiInvalidResponse):
            self.run_one(bad)

    def test_empty_definition_or_example(self):
        for field in ("definition", "example"):
            bad = synonym_q()
            bad["correct"][field] = "  "
            with self.assertRaises(gs.GeminiInvalidResponse, msg=field):
                self.run_one(bad)

    def test_option_must_be_single_word(self):
        bad = synonym_q()
        bad["correct"]["word"] = "look up to"
        with self.assertRaises(gs.GeminiInvalidResponse):
            self.run_one(bad)

    def test_blank_sentence_rules(self):
        cases = {
            "no blank": "I really love her courage in hard times.",
            "two blanks": "I ___ her and ___ him in hard times.",
            "leaks answer": "I ___ her, and admire her courage always.",
            "leaks distractor": "I ___ her and enroll in her class.",
            "too short": "I ___ it.",
        }
        for name, sentence in cases.items():
            with self.assertRaises(gs.GeminiInvalidResponse, msg=name):
                self.run_one(blank_q(sentence=sentence))

    def test_blank_answer_must_be_requested_word(self):
        bad = blank_q()
        bad["correct"] = info("esteem")
        with self.assertRaises(gs.GeminiInvalidResponse):
            self.run_one(bad)

    def test_long_underscores_are_normalised(self):
        result = self.run_one(blank_q(sentence="I really ______ her courage in hard times."))
        self.assertEqual(result.questions[0]["questionText"], "I really ___ her courage in hard times.")

    def test_all_invalid_raises_and_reports(self):
        bad = synonym_q()
        bad["distractors"] = []
        with self.assertRaises(gs.GeminiInvalidResponse) as ctx:
            self.run_one(bad)
        self.assertEqual(len(ctx.exception.rejected), 1)


class FailureTests(ServiceTestCase):
    def test_retries_quota_then_succeeds(self):
        client = fake_client(api_error(429), payload(synonym_q()))
        result = gs.generate_questions(["admire"], "EASY", ["SYNONYM"], client=client, model="m")
        self.assertEqual(client.models.calls, 2)
        self.assertEqual(len(result.questions), 1)

    def test_gives_up_after_max_attempts(self):
        client = fake_client(*[api_error(503)] * gs.MAX_ATTEMPTS)
        with self.assertRaises(gs.GeminiAPIError) as ctx:
            gs.generate_questions(["admire"], "EASY", client=client, model="m")
        self.assertEqual(client.models.calls, gs.MAX_ATTEMPTS)
        self.assertTrue(ctx.exception.retryable)

    def test_wrong_model_is_not_retried(self):
        client = fake_client(api_error(404))
        with self.assertRaises(gs.GeminiAPIError) as ctx:
            gs.generate_questions(["admire"], "EASY", client=client, model="m")
        self.assertEqual(client.models.calls, 1)
        self.assertIn("GEMINI_MODEL", ctx.exception.user_message)

    def test_bad_key_is_not_retried(self):
        client = fake_client(api_error(403))
        with self.assertRaises(gs.GeminiAPIError):
            gs.generate_questions(["admire"], "EASY", client=client, model="m")
        self.assertEqual(client.models.calls, 1)

    def test_malformed_json_retries_then_fails(self):
        client = fake_client("not json", "{ also bad", "")
        with self.assertRaises(gs.GeminiInvalidResponse):
            gs.generate_questions(["admire"], "EASY", client=client, model="m")
        self.assertEqual(client.models.calls, gs.MAX_ATTEMPTS)

    def test_malformed_json_then_good(self):
        client = fake_client("not json", payload(synonym_q()))
        result = gs.generate_questions(["admire"], "EASY", ["SYNONYM"], client=client, model="m")
        self.assertEqual(len(result.questions), 1)

    def test_wrong_json_shape_fails(self):
        client = fake_client(*[json.dumps({"questions": "nope"})] * gs.MAX_ATTEMPTS)
        with self.assertRaises(gs.GeminiInvalidResponse):
            gs.generate_questions(["admire"], "EASY", client=client, model="m")

    def test_missing_api_key(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), mock.patch.object(gs, "load_dotenv"):
            with self.assertRaises(gs.GeminiConfigError):
                gs.get_client()


class InputTests(ServiceTestCase):
    def test_bad_inputs(self):
        client = fake_client()
        with self.assertRaises(ValueError):
            gs.generate_questions(["admire"], "EXTREME", client=client)
        with self.assertRaises(ValueError):
            gs.generate_questions(["admire"], "EASY", ["TRIVIA"], client=client)
        with self.assertRaises(ValueError):
            gs.generate_questions([], "EASY", client=client)
        with self.assertRaises(ValueError):
            gs.generate_questions(["two words"], "EASY", client=client)
        with self.assertRaises(ValueError):
            gs.generate_questions([f"word{c}" for c in "abcdefghijklm"], "EASY", client=client)
        self.assertEqual(client.models.calls, 0)  # nothing was sent to Gemini


class SuggestWordsTests(ServiceTestCase):
    def test_filters_excluded_invalid_and_duplicates(self):
        reply = json.dumps({"words": ["Esteem", "admire", "two words", "belie", "belie", "x1", "startling"]})
        words = gs.suggest_words("MEDIUM", 3, {"admire"}, client=fake_client(reply), model="m")
        self.assertEqual(words, ["esteem", "belie", "startling"])

    def test_nothing_usable_raises(self):
        reply = json.dumps({"words": ["admire", "???"]})
        with self.assertRaises(gs.GeminiInvalidResponse):
            gs.suggest_words("EASY", 3, {"admire"}, client=fake_client(reply), model="m")


# --------------------------------------------------------------------------
# New question types, chunking and topics
# --------------------------------------------------------------------------

def meaning_q(word="admire"):
    return {
        "target_word": word, "question_type": "MEANING", "sentence": "",
        "target": info(word, "to respect someone"), "correct": info(word, "to respect and like someone a lot"),
        "distractors": [info("repair", "to fix something that is broken"),
                        info("travel", "to go to a faraway place"),
                        info("inform", "to give someone the facts")],
    }


def odd_q(word="joyful"):
    return {
        "target_word": word, "question_type": "ODD_ONE_OUT", "sentence": "feeling happy",
        "target": info(word), "correct": info("table"),
        "distractors": [info(word), info("glad"), info("pleased")],
    }


def form_q(word="admire", sentence="Her ___ for the artist grew every year."):
    return {
        "target_word": word, "question_type": "WORD_FORM", "sentence": sentence,
        "target": info(word), "correct": info("admiration"),
        "distractors": [info("admirable"), info("admiring")],
    }


def spelling_q(word="accommodate"):
    note = lambda w, n: {"word": w, "definition": n, "example": ""}
    return {
        "target_word": word, "question_type": "SPELLING", "sentence": "",
        "target": info(word, "to provide a place for someone to stay"), "correct": info(word),
        "distractors": [note("acommodate", "Missing the double c"), note("accomodate", "Missing the double m"),
                        note("accommodete", "The ending is -ate")],
    }


class NewTypeTests(ServiceTestCase):
    def one(self, raw, qtype, word):
        client = fake_client(payload(raw))
        return gs.generate_questions([word], "MEDIUM", [qtype], client=client, model="m").questions[0]

    def rejected(self, raw, qtype, word):
        client = fake_client(payload(raw))
        with self.assertRaises(gs.GeminiInvalidResponse) as ctx:
            gs.generate_questions([word], "MEDIUM", [qtype], client=client, model="m")
        return ctx.exception.rejected[0]["reason"]

    # MEANING
    def test_meaning_question(self):
        q = self.one(meaning_q(), "MEANING", "admire")
        self.assertEqual(q["questionText"], "What does admire mean?")
        self.assertEqual(q["correctAnswer"], "to respect and like someone a lot")
        self.assertEqual(q["distractors"][0], "to fix something that is broken")
        self.assertEqual(q["explanation"]["distractors"][0]["word"], "repair")
        self.assertEqual(q["explanation"]["correct"]["relation"], "means")
        self.assertEqual(q["uniqueKey"], "admire:MEANING:admire")

    def test_meaning_rejects_definition_with_the_word(self):
        bad = meaning_q()
        bad["distractors"][0]["definition"] = "to admire a machine again"
        self.assertIn("contains the requested word", self.rejected(bad, "MEANING", "admire"))

    def test_meaning_rejects_same_definitions(self):
        bad = meaning_q()
        bad["distractors"][1]["definition"] = "To respect and like someone a lot"
        self.assertIn("not distinct", self.rejected(bad, "MEANING", "admire"))

    def test_meaning_answer_must_describe_the_word(self):
        bad = meaning_q()
        bad["correct"] = info("esteem")
        self.assertIn("requested word", self.rejected(bad, "MEANING", "admire"))

    def test_meaning_accepts_two_distractors(self):
        ok = meaning_q()
        ok["distractors"] = ok["distractors"][:2]
        self.assertEqual(len(self.one(ok, "MEANING", "admire")["distractors"]), 2)

    # ODD_ONE_OUT
    def test_odd_one_out_question(self):
        q = self.one(odd_q(), "ODD_ONE_OUT", "joyful")
        self.assertEqual(q["questionText"], "Which word does not belong with the others?")
        self.assertEqual(q["correctAnswer"], "table")
        self.assertEqual(sorted(q["distractors"]), ["glad", "joyful", "pleased"])
        self.assertEqual(q["explanation"]["theme"], "feeling happy")

    def test_odd_one_out_needs_target_in_the_group(self):
        bad = odd_q()
        bad["distractors"][0] = info("cheerful")
        self.assertIn("one of the three related words", self.rejected(bad, "ODD_ONE_OUT", "joyful"))

    def test_odd_one_out_needs_three_related_words_and_a_theme(self):
        bad = odd_q()
        bad["distractors"] = bad["distractors"][:2]
        self.assertIn("needs 3 distractors", self.rejected(bad, "ODD_ONE_OUT", "joyful"))
        bad = odd_q()
        bad["sentence"] = ""
        self.assertIn("theme", self.rejected(bad, "ODD_ONE_OUT", "joyful"))

    # WORD_FORM
    def test_word_form_question(self):
        q = self.one(form_q(), "WORD_FORM", "admire")
        self.assertEqual(q["questionText"], "Her ___ for the artist grew every year.")
        self.assertEqual(q["correctAnswer"], "admiration")
        self.assertEqual(q["distractors"], ["admirable", "admiring"])
        self.assertTrue(q["uniqueKey"].startswith("admire:WORD_FORM:admiration:"))

    def test_word_form_rejects_unrelated_option(self):
        bad = form_q()
        bad["distractors"][0] = info("careful")
        self.assertIn("form of the requested word", self.rejected(bad, "WORD_FORM", "admire"))

    def test_word_form_sentence_rules(self):
        self.assertIn("exactly one blank", self.rejected(form_q(sentence="Her love for art grew every year."), "WORD_FORM", "admire"))
        self.assertIn("already contains", self.rejected(form_q(sentence="Her ___ and admirable work grew every year."), "WORD_FORM", "admire"))

    # SPELLING
    def test_spelling_question(self):
        q = self.one(spelling_q(), "SPELLING", "accommodate")
        self.assertEqual(q["questionText"],
                         "Which is the correct spelling of the word that means \u201cto provide a place for someone to stay\u201d?")
        self.assertEqual(q["correctAnswer"], "accommodate")
        self.assertEqual(q["distractors"], ["acommodate", "accomodate", "accommodete"])
        self.assertEqual(q["explanation"]["distractors"][1]["definition"], "Missing the double m")
        self.assertEqual(q["explanation"]["distractors"][1]["example"], "")

    def test_spelling_rejects_bad_misspellings(self):
        far = spelling_q(); far["distractors"][0]["word"] = "acomodation"      # too many changes
        self.assertIn("plausible misspelling", self.rejected(far, "SPELLING", "accommodate"))
        other = spelling_q(); other["distractors"][0]["word"] = "ccommodate"    # different first letter
        self.assertIn("plausible misspelling", self.rejected(other, "SPELLING", "accommodate"))

    def test_spelling_rejects_short_words_and_leaky_definitions(self):
        short = spelling_q("tree")
        short["distractors"] = [{"word": w, "definition": "A mistake in spelling", "example": ""} for w in ("tre", "trea", "treee")]
        self.assertIn("too short", self.rejected(short, "SPELLING", "tree"))
        leak = spelling_q()
        leak["target"]["definition"] = "to accommodate a guest"
        self.assertIn("contains the requested word", self.rejected(leak, "SPELLING", "accommodate"))

    def test_edit_distance(self):
        self.assertEqual(gs._edit_distance("accommodate", "accomodate"), 1)
        self.assertEqual(gs._edit_distance("kitten", "sitting"), 3)
        self.assertEqual(gs._edit_distance("", "abc"), 3)


class ChunkingTests(ServiceTestCase):
    words = ["alpha", "bravo", "cedar", "delta", "ember"]

    def test_calls_needed(self):
        self.assertEqual(gs.calls_needed(5, 7), 3)     # 2 words per call
        self.assertEqual(gs.calls_needed(1, 7), 1)
        self.assertEqual(gs.calls_needed(5, 3), 2)     # 4 words per call
        self.assertEqual(gs.calls_needed(3, 1), 1)

    def test_large_batches_are_split_into_calls(self):
        chunks = [payload(synonym_q("alpha"), synonym_q("bravo")),
                  payload(synonym_q("cedar"), synonym_q("delta")),
                  payload(synonym_q("ember"))]
        client = fake_client(*chunks)
        result = gs.generate_questions(self.words, "EASY", client=client, model="m")
        self.assertEqual(client.models.calls, 3)
        self.assertEqual(len(result.questions), 5)
        # each call only mentions its own words
        self.assertIn('"cedar"', client.models.prompts[1])
        self.assertNotIn('"alpha"', client.models.prompts[1])

    def test_failed_call_still_returns_the_other_results(self):
        client = fake_client(payload(synonym_q("alpha")), "bad", "bad", "bad", payload(synonym_q("ember")))
        result = gs.generate_questions(self.words, "EASY", client=client, model="m")
        self.assertEqual(sorted(q["word"] for q in result.questions), ["alpha", "ember"])
        self.assertIn(("cedar", "SYNONYM"), result.missing)

    def test_api_failure_stops_further_calls(self):
        client = fake_client(payload(synonym_q("alpha")), api_error(403))
        result = gs.generate_questions(self.words, "EASY", client=client, model="m")
        self.assertEqual(client.models.calls, 2)
        self.assertEqual([q["word"] for q in result.questions], ["alpha"])

    def test_every_call_failing_raises_the_last_error(self):
        client = fake_client(api_error(403))
        with self.assertRaises(gs.GeminiAPIError):
            gs.generate_questions(self.words, "EASY", client=client, model="m")

    def test_prompt_lists_only_requested_type_rules(self):
        prompt = gs.build_generation_prompt(["admire"], "EASY", ("SPELLING", "MEANING"))
        self.assertIn("- SPELLING:", prompt)
        self.assertIn("- MEANING:", prompt)
        self.assertNotIn("- ANTONYM:", prompt)


class TopicTests(ServiceTestCase):
    def test_normalize_topic(self):
        self.assertIsNone(gs.normalize_topic(None))
        self.assertIsNone(gs.normalize_topic(""))
        self.assertIsNone(gs.normalize_topic("General"))
        self.assertEqual(gs.normalize_topic(" Business "), "business")
        for bad in ("cooking", 5, "business;drop"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                gs.normalize_topic(bad)

    def test_topic_goes_into_the_suggest_prompt(self):
        client = fake_client(json.dumps({"words": ["merger", "invoice", "tariff"]}))
        words = gs.suggest_words("MEDIUM", 3, set(), topic="business", client=client, model="m")
        self.assertEqual(words, ["merger", "invoice", "tariff"])
        self.assertIn("business, finance", client.models.prompts[0])

    def test_no_topic_line_without_a_topic(self):
        self.assertNotIn("Topic:", gs.build_suggest_prompt("EASY", 3, []))

    def test_unknown_topic_never_reaches_gemini(self):
        client = fake_client()
        with self.assertRaises(ValueError):
            gs.suggest_words("EASY", 3, set(), topic="cooking", client=client, model="m")
        self.assertEqual(client.models.calls, 0)


if __name__ == "__main__":
    unittest.main()