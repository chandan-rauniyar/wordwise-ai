"""Round builder and answer checker (Phase 5).

* build_round(): picks 5 questions (word questions, revision questions, fillers),
  refills the bank from Gemini when it runs low, and returns questions WITHOUT
  the correct answer plus a signed round token.
* check_answer(): validates an answer on the server and returns the explanation.

Three ways to start a round:
  - one typed word          questions about that word first, then fillers
  - a word list (browser)   questions about the user's own words only
  - a mix (no word)         random unseen questions, optionally from one topic

The server keeps no state about the user. Which questions the user has seen or
got wrong, and their word list, are sent by the browser; the round itself is
described by a signed token.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Iterable, Optional

from django.core import signing
from pymongo.errors import PyMongoError

from services import question_bank as bank
from services.gemini_service import (DIFFICULTIES, SUPPORTED_TYPES, GeminiError, normalize_topic,
                                     normalize_word)

logger = logging.getLogger(__name__)

ROUND_SIZE = 5
MAX_REVIEW = 2            # wrong questions from earlier rounds that may return in one round
MAX_WORD_QUESTIONS = 3    # questions about the word the user typed
MAX_LIST_WORDS = 20       # words used from a user's word list
MAX_SYNC_LIST_WORDS = 2   # list words generated while the user waits (the rest in the background)
LOW_WATERMARK = 15        # refill in the background when fewer unseen questions remain
POINTS_PER_CORRECT = 100
TOKEN_SALT = "wordwise.round"
TOKEN_MAX_AGE_SECONDS = 2 * 60 * 60
BACKGROUND_REFILL = True  # tests switch this off
FAILED_RETRY_SECONDS = 60 * 60  # do not ask Gemini again for a list word that failed, for this long

_failed_words: dict = {}  # word -> time.monotonic() of the last failed attempt (this process only)

TYPE_ORDER = {t: i for i, t in enumerate(SUPPORTED_TYPES)}
# How many wrong options are shown next to the correct one (default 1 = two choices)
SHOWN_DISTRACTORS = {"ODD_ONE_OUT": 3, "SPELLING": 2, "WORD_FORM": 2}


class NotEnoughQuestions(Exception):
    user_message = "No questions are available yet. Please try again in a moment."

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.user_message)
        if message:
            self.user_message = message


class InvalidRound(Exception):
    """Bad, expired or tampered round token, or an unknown question."""

    user_message = "This round is no longer valid. Please start a new round."


class InvalidAnswer(Exception):
    user_message = "That answer is not one of the choices."


def _key(text: str) -> str:
    """Comparison key for answers (case and spacing do not matter)."""
    return " ".join(text.split()).casefold()


# --------------------------------------------------------------------------
# Picking questions
# --------------------------------------------------------------------------

def _pick_varied(candidates: list, need: int, used_words: set, used_types: set) -> list:
    """Choose `need` questions, preferring new words and new question types.

    `used_words` and `used_types` are updated with what was picked."""
    picks, picked_ids = [], set()
    rules = (
        lambda d: d["normalizedWord"] not in used_words and d["questionType"] not in used_types,
        lambda d: d["normalizedWord"] not in used_words,
        lambda d: True,
    )
    for rule in rules:
        for doc in candidates:
            if len(picks) == need:
                return picks
            if doc["_id"] in picked_ids or not rule(doc):
                continue
            picks.append(doc)
            picked_ids.add(doc["_id"])
            used_words.add(doc["normalizedWord"])
            used_types.add(doc["questionType"])
    return picks


def _pick_fillers(db, difficulty: Optional[str], exclude: set, need: int, used_words: set,
                  used_types: set, topic: Optional[str] = None) -> list:
    """Random questions, preferring words and question types not used yet in this round."""
    candidates = bank.find_unseen(db, difficulty, exclude, limit=need * 6, topic=topic)
    return _pick_varied(candidates, need, used_words, used_types)


def _in_background(name: str, job) -> None:
    def work():
        try:
            job()
        except Exception:  # never let a background job crash the server
            logger.warning("Background job %s failed", name, exc_info=True)

    threading.Thread(target=work, name=name, daemon=True).start()


def _clean_word_list(words) -> tuple:
    """Normalised, de-duplicated list words plus the entries that were skipped."""
    if not isinstance(words, (list, tuple)):
        raise ValueError("The word list must be a list of words.")
    clean, skipped = [], []
    for item in list(words)[: MAX_LIST_WORDS * 2]:
        try:
            word = normalize_word(item)
        except ValueError:
            skipped.append(str(item)[:30])
            continue
        if word not in clean:
            clean.append(word)
    return clean[:MAX_LIST_WORDS], skipped[:5]


def _needing_questions(db, words: list) -> list:
    """List words that still need Gemini and did not fail recently."""
    now = time.monotonic()
    return [w for w in bank.incomplete_words(db, words) if now - _failed_words.get(w, -FAILED_RETRY_SECONDS) >= FAILED_RETRY_SECONDS]


def _generate_list_words(db, words: list, difficulty: str) -> list:
    """Generate questions for list words; returns the words that could not be created."""
    bank.ensure_words_questions(db, words, difficulty, strict=False)
    failed = bank.incomplete_words(db, words)
    for word in failed:
        _failed_words[word] = time.monotonic()
    return failed


def _list_round_questions(db, list_words: list, difficulty: str, seen: set, taken: set, need: int) -> tuple:
    """Questions for the user's own word list.

    Returns (questions, reused_seen_questions, words_that_failed)."""
    def pool(exclude):
        docs = [d for d in bank.find_by_words(db, list_words) if d["_id"] not in exclude]
        random.shuffle(docs)
        return docs

    failed: list = []
    unseen = pool(seen | taken)
    if len(unseen) < need:
        todo = _needing_questions(db, list_words)
        if todo:  # generate a couple of words now; the rest in the background
            failed = _generate_list_words(db, todo[:MAX_SYNC_LIST_WORDS], difficulty)
            unseen = pool(seen | taken)
    rest = _needing_questions(db, list_words)
    if rest and BACKGROUND_REFILL:
        batch = rest[:8]
        _in_background("list-words", lambda: _generate_list_words(db, batch, difficulty))

    used_words: set = set()
    used_types: set = set()
    picked = _pick_varied(unseen, need, used_words, used_types)
    reused = False
    if len(picked) < need:  # not enough new questions: allow ones already seen
        have = taken | {d["_id"] for d in picked}
        again = _pick_varied([d for d in pool(taken) if d["_id"] not in have], need - len(picked),
                             used_words, used_types)
        reused = bool(again)
        picked += again
    return picked, reused, failed


# --------------------------------------------------------------------------
# Building a round
# --------------------------------------------------------------------------

def build_round(db, *, word: Optional[str] = None, difficulty: str, seen_ids, wrong_ids,
                topic: Optional[str] = None, words: Optional[Iterable] = None) -> dict:
    """Return {"roundToken", "questions", "notice"} for a new round.

    Raises ValueError (bad word, word list, topic or difficulty), GeminiError,
    bank.GenerationBusy, bank.DailyLimitReached, NotEnoughQuestions.
    """
    difficulty = (difficulty or "EASY").strip().upper()
    if difficulty not in DIFFICULTIES:
        raise ValueError("Choose Easy, Medium or Hard.")
    topic = normalize_topic(topic)

    list_words, skipped = None, []
    if words:
        list_words, skipped = _clean_word_list(words)
        if not list_words:
            raise ValueError("Add at least one valid word (letters only) to your list.")
    normalized = None
    if not list_words and word and word.strip():
        normalized = normalize_word(word)

    seen = set(bank.to_object_ids(seen_ids, 500))
    wrong = bank.to_object_ids(wrong_ids, 100)
    notes: list = []

    # 1. Revision: questions the user got wrong before (limited, shown after other questions)
    review_docs = bank.get_many(db, wrong)
    random.shuffle(review_docs)
    review_docs = review_docs[:MAX_REVIEW]
    review_ids = {d["_id"] for d in review_docs}
    main_needed = ROUND_SIZE - len(review_docs)

    main: list = []
    refilled = False

    if list_words:
        # 2a. Word list: only questions about the user's own words
        main, reused, failed = _list_round_questions(db, list_words, difficulty, seen, review_ids, main_needed)
        if reused:
            notes.append("You have seen most questions for your list, so some may repeat.")
        if failed and main:
            notes.append("We could not create questions for: " + ", ".join(failed) + ".")
        if not main and not review_docs:
            raise NotEnoughQuestions("We could not create questions for the words in your list. "
                                     "Check the spelling or try other words.")
        if skipped:
            notes.append("Skipped: " + ", ".join(skipped) + ".")
    else:
        # 2b. Typed word: questions about it (generated only if missing)
        if normalized:
            word_docs = [d for d in bank.ensure_word_questions(db, normalized, difficulty)
                         if d["_id"] not in review_ids]
            random.shuffle(word_docs)
            word_docs.sort(key=lambda d: d["_id"] in seen)  # unseen first, random type order
            main = word_docs[: min(MAX_WORD_QUESTIONS, main_needed)]
            main.sort(key=lambda d: TYPE_ORDER.get(d["questionType"], 99))

        # 3. Fill the round with random unseen questions of the chosen difficulty (and topic)
        need = main_needed - len(main)
        if need > 0:
            taken = review_ids | {d["_id"] for d in main}
            used_words = {d["normalizedWord"] for d in main}
            used_types = {d["questionType"] for d in main}
            fillers = _pick_fillers(db, difficulty, seen | taken, need, used_words, used_types, topic)
            if len(fillers) < need:
                refilled = True
                try:
                    bank.refill(db, difficulty, topic, words=bank.SYNC_REFILL_WORDS)
                except (GeminiError, bank.GenerationBusy, bank.DailyLimitReached, PyMongoError) as exc:
                    logger.warning("Synchronous refill failed: %s", exc)
                fillers = _pick_fillers(db, difficulty, seen | taken, need, used_words, used_types, topic)
            if len(fillers) < need:  # still short: allow questions the user has already seen
                have = taken | {d["_id"] for d in fillers}
                more = _pick_fillers(db, difficulty, have, need - len(fillers), used_words, used_types, topic)
                if len(fillers) + len(more) < need:
                    more += _pick_fillers(db, None, have | {d["_id"] for d in more},
                                          need - len(fillers) - len(more), used_words, used_types, topic)
                if more:
                    notes.append("You have seen most of our questions, so some may repeat.")
                fillers += more
            main += fillers

        if not main and not review_docs:
            if topic:
                raise NotEnoughQuestions("We could not find questions for this topic yet. Please try again in a moment.")
            raise NotEnoughQuestions()

        # 4. Keep the bank stocked: refill in the background when few unseen questions remain
        if BACKGROUND_REFILL and not refilled:
            used = seen | review_ids | {d["_id"] for d in main}
            if bank.count_unseen(db, difficulty, used, topic) < LOW_WATERMARK:
                _in_background(f"refill-{difficulty}", lambda: bank.refill(db, difficulty, topic))

    # 5. Order: revision questions come after at least two other questions
    ordered = list(main)
    for k, doc in enumerate(review_docs):
        ordered.insert(min(2 + 2 * k, len(ordered)), doc)

    public, shown_map = [], {}
    for doc in ordered:
        count = min(SHOWN_DISTRACTORS.get(doc["questionType"], 1), len(doc["distractors"]))
        shown = random.sample(doc["distractors"], count)
        options = [doc["correctAnswer"]] + shown
        random.shuffle(options)
        qid = str(doc["_id"])
        shown_map[qid] = shown
        public.append({
            "id": qid,
            "type": doc["questionType"],
            "text": doc["questionText"],
            "options": options,
            "review": doc["_id"] in review_ids,
        })

    token = signing.dumps({"q": [q["id"] for q in public], "d": shown_map}, salt=TOKEN_SALT, compress=True)
    return {"roundToken": token, "questions": public, "notice": " ".join(notes) or None}


# --------------------------------------------------------------------------
# Checking an answer
# --------------------------------------------------------------------------

def build_explanation(doc: dict, shown) -> dict:
    """The 'Learn why' content shown after answering and on the results page.

    `shown` is the list of wrong options that were displayed. Each section may carry the
    `word` it is about, so the browser can offer to pronounce it."""
    shown = [shown] if isinstance(shown, str) else list(shown or [])
    shown_keys = {_key(s) for s in shown}
    exp, qtype, word = doc["explanation"], doc["questionType"], doc["word"]
    correct = exp["correct"]
    distractors = [d for d in exp["distractors"]
                   if _key(d["word"]) in shown_keys or _key(d["definition"]) in shown_keys]
    if not distractors:
        distractors = exp["distractors"][:1]

    def part(heading, info, speak=None):
        return {"heading": heading, "text": info["definition"], "example": info.get("example", ""), "word": speak}

    answer_sentence = None
    if qtype in ("SYNONYM", "ANTONYM"):
        title = f"Word similar to {word}?" if qtype == "SYNONYM" else f"Word opposite of {word}?"
        sections = [
            part(f"What's the definition of {word}?", exp["target"], word),
            part(f"How is {correct['word']} {correct['relation']}?", correct, correct["word"]),
        ] + [part(f"What's the definition of {d['word']}?", d, d["word"]) for d in distractors]

    elif qtype == "FILL_BLANK":
        title = doc["questionText"]
        sections = [part(f"What does {word} mean?", correct, word)] + \
                   [part(f"What does {d['word']} mean?", d, d["word"]) for d in distractors]
        answer_sentence = doc["questionText"].replace("___", word)

    elif qtype == "MEANING":
        title = doc["questionText"]
        sections = [part(f"How is {word} used?", correct, word)] + \
                   [part(f"What does {d['word']} mean?", d, d["word"]) for d in distractors]

    elif qtype == "ODD_ONE_OUT":
        group = sorted([correct["word"]] + [d["word"] for d in distractors])
        title = "Odd one out: " + ", ".join(group)
        sections = [part(f"Why does {correct['word']} not belong?", correct, correct["word"])] + \
                   [part(f"What does {d['word']} mean?", d, d["word"]) for d in distractors]
        answer_sentence = f"The other words are all about {exp.get('theme', 'the same idea')}."

    elif qtype == "WORD_FORM":
        title = doc["questionText"]
        sections = [part(f"What does {correct['word']} mean?", correct, correct["word"])] + \
                   [part(f"What does {d['word']} mean?", d, d["word"]) for d in distractors]
        answer_sentence = doc["questionText"].replace("___", correct["word"])

    else:  # SPELLING
        title = f"Spelling: {word}"
        sections = [part(f"What does {word} mean?", exp["target"], word)] + \
                   [{"heading": f"Why is {d['word']} wrong?", "text": d["definition"], "example": "", "word": None}
                    for d in distractors]

    return {"title": title, "sections": sections, "answerSentence": answer_sentence}


def check_answer(db, *, token: str, question_id: str, chosen: Optional[str]) -> dict:
    """Check one answer. `chosen=None` means the user skipped.

    Raises InvalidRound or InvalidAnswer.
    """
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.BadSignature:  # includes expired tokens
        raise InvalidRound()
    if not isinstance(question_id, str) or question_id not in data.get("q", []):
        raise InvalidRound()

    docs = bank.get_many(db, bank.to_object_ids([question_id]))
    if not docs:
        raise InvalidRound()
    doc = docs[0]
    shown = data["d"][question_id]
    shown = [shown] if isinstance(shown, str) else list(shown)

    skipped = chosen is None
    if not skipped:
        if not isinstance(chosen, str):
            raise InvalidAnswer()
        if _key(chosen) not in {_key(o) for o in [doc["correctAnswer"]] + shown}:
            raise InvalidAnswer()

    is_correct = (not skipped) and _key(chosen) == _key(doc["correctAnswer"])
    return {
        "correct": is_correct,
        "skipped": skipped,
        "points": POINTS_PER_CORRECT if is_correct else 0,
        "correctAnswer": doc["correctAnswer"],
        "explanation": build_explanation(doc, shown),
    }