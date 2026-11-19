"""Question bank: stores and finds vocabulary questions in MongoDB (Phase 4).

Rules
-----
* MongoDB is always checked first. Gemini is called only when content is missing.
* Only validated questions (from gemini_service) are saved.
* A unique index on `uniqueKey` stops duplicates; a lock stops two requests from
  generating the same thing at once.
* Nothing about users is stored here, only vocabulary content.

Every function takes the database as its first argument so it can be tested
with an in-memory database.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from bson import ObjectId
from pymongo.errors import BulkWriteError, DuplicateKeyError

from services import gemini_service
from services.gemini_service import SUPPORTED_TYPES, calls_needed, normalize_topic, normalize_word

logger = logging.getLogger(__name__)

COMPLETE_VERSION = 2        # a word with any contentVersion >= 2 question already has all question types
REFILL_WORDS = 4            # words per background refill (4 words x 7 types = up to 28 questions)
SYNC_REFILL_WORDS = 2       # smaller refill when a user is waiting for it
LOCK_TTL_SECONDS = 180
LOCK_WAIT_SECONDS = 45


class GenerationBusy(Exception):
    """Another request is already generating this content and it did not finish in time."""

    user_message = "Questions for this are still being prepared. Please try again in a moment."


class DailyLimitReached(Exception):
    """The daily cap on Gemini calls has been used up."""

    user_message = "We have reached today's limit for creating new questions. Please try again tomorrow."


# --------------------------------------------------------------------------
# Daily Gemini budget (in this process; protects the API quota)
# --------------------------------------------------------------------------

_budget_lock = threading.Lock()
_budget = {"day": None, "calls": 0}


def _spend_gemini_call(calls: int = 1) -> None:
    limit = int(os.getenv("GEMINI_DAILY_CALL_LIMIT", "300"))
    today = date.today()
    with _budget_lock:
        if _budget["day"] != today:
            _budget["day"], _budget["calls"] = today, 0
        if _budget["calls"] + calls > limit:
            raise DailyLimitReached()
        _budget["calls"] += calls


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def to_object_ids(values: Iterable, limit: int = 500) -> list:
    """Convert client-sent id strings to ObjectIds, silently skipping bad ones."""
    ids, seen = [], set()
    for value in list(values or [])[:limit]:
        if isinstance(value, str) and len(value) == 24:
            try:
                oid = ObjectId(value)
            except Exception:
                continue
            if oid not in seen:
                seen.add(oid)
                ids.append(oid)
    return ids


def find_by_word(db, normalized_word: str) -> list:
    return list(db.questions.find({"normalizedWord": normalized_word}))


def get_many(db, ids: list) -> list:
    """Fetch questions by id, keeping the order of `ids`."""
    if not ids:
        return []
    found = {doc["_id"]: doc for doc in db.questions.find({"_id": {"$in": ids}})}
    return [found[i] for i in ids if i in found]


def find_by_words(db, normalized_words: list) -> list:
    return list(db.questions.find({"normalizedWord": {"$in": list(normalized_words)}}))


def _unseen_query(difficulty: Optional[str], exclude_ids: Iterable, topic: Optional[str]) -> dict:
    query: dict = {"_id": {"$nin": list(exclude_ids)}}
    if difficulty:
        query["difficulty"] = difficulty
    if topic:
        query["topic"] = topic
    return query


def find_unseen(db, difficulty: Optional[str], exclude_ids: Iterable, limit: int,
                topic: Optional[str] = None) -> list:
    """Random questions not in `exclude_ids` (any difficulty if `difficulty` is None).

    With `topic`, only questions generated for that topic are returned."""
    ids = [d["_id"] for d in db.questions.find(_unseen_query(difficulty, exclude_ids, topic), {"_id": 1})]
    random.shuffle(ids)
    return get_many(db, ids[:limit])


def count_unseen(db, difficulty: str, exclude_ids: Iterable, topic: Optional[str] = None) -> int:
    return db.questions.count_documents(_unseen_query(difficulty, exclude_ids, topic))


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def save_questions(db, docs: list) -> tuple:
    """Insert validated questions. Returns (inserted, duplicates_skipped)."""
    if not docs:
        return 0, 0
    now = datetime.now(timezone.utc)
    prepared = [{**doc, "createdAt": now, "updatedAt": now} for doc in docs]
    try:
        result = db.questions.insert_many(prepared, ordered=False)
        return len(result.inserted_ids), 0
    except BulkWriteError as exc:
        errors = exc.details.get("writeErrors", [])
        if any(err.get("code") != 11000 for err in errors):
            raise
        return exc.details.get("nInserted", 0), len(errors)


# --------------------------------------------------------------------------
# Locks (so two requests do not generate the same content twice)
# --------------------------------------------------------------------------

def acquire_lock(db, key: str, ttl_seconds: int = LOCK_TTL_SECONDS) -> bool:
    now = datetime.now(timezone.utc)
    db.generation_locks.delete_many({"_id": key, "expireAt": {"$lt": now}})  # TTL monitor can lag
    try:
        db.generation_locks.insert_one({"_id": key, "createdAt": now, "expireAt": now + timedelta(seconds=ttl_seconds)})
        return True
    except DuplicateKeyError:
        return False


def release_lock(db, key: str) -> None:
    db.generation_locks.delete_one({"_id": key})


# --------------------------------------------------------------------------
# Get-or-generate
# --------------------------------------------------------------------------

def _missing_types(existing: list) -> Optional[tuple]:
    """None when the word is complete, otherwise the question types still to generate.

    Words generated by this version have every question type (contentVersion 2).
    Words from the first version only have synonym/antonym/fill-blank, so only the
    new types are generated for them."""
    if any(doc.get("contentVersion", 1) >= COMPLETE_VERSION for doc in existing):
        return None
    have = {doc["questionType"] for doc in existing}
    return tuple(t for t in SUPPORTED_TYPES if t not in have) or None


def incomplete_words(db, words: Iterable) -> list:
    """The words (in order) that still need questions from Gemini."""
    by_word: dict = {}
    for doc in find_by_words(db, list(words)):
        by_word.setdefault(doc["normalizedWord"], []).append(doc)
    return [w for w in words if _missing_types(by_word.get(w, [])) is not None]


def ensure_words_questions(db, words: Iterable, difficulty: str, *, strict: bool = True) -> dict:
    """Return {word: stored questions}, generating with Gemini only what is missing.

    MongoDB is checked first: a word that already has its questions never reaches Gemini.
    With strict=False, Gemini problems are logged and skipped instead of raised.

    Raises ValueError (bad word), gemini_service.GeminiError, GenerationBusy,
    DailyLimitReached. Nothing is saved when a generation call fails.
    """
    normalized = list(dict.fromkeys(normalize_word(w) for w in words))
    locked, todo, waiting = [], {}, []
    try:
        for word in normalized:
            if _missing_types(find_by_word(db, word)) is None:
                continue  # reuse: no Gemini call
            key = f"word:{word}"
            if acquire_lock(db, key):
                locked.append(key)
                missing = _missing_types(find_by_word(db, word))  # re-check after getting the lock
                if missing is not None:
                    todo[word] = missing
            else:
                waiting.append(word)

        groups: dict = {}
        for word, missing in todo.items():
            groups.setdefault(missing, []).append(word)
        for missing, group in groups.items():
            try:
                _spend_gemini_call(calls_needed(len(group), len(missing)))
                result = gemini_service.generate_questions(group, difficulty, missing)
            except (gemini_service.GeminiError, DailyLimitReached) as exc:
                # Words that already have some stored questions (first-version words waiting for
                # the new types) keep working with what they have; only a word with nothing fails.
                if strict and any(not find_by_word(db, word) for word in group):
                    raise
                logger.warning("Could not generate %s: %s", group, exc)
                continue
            inserted, duplicates = save_questions(db, result.questions)
            logger.info("Generated words=%s saved=%d duplicates=%d rejected=%d",
                        group, inserted, duplicates, len(result.rejected))
    finally:
        for key in locked:
            release_lock(db, key)

    # words that another request is generating right now: wait for them
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    for word in waiting:
        while _missing_types(find_by_word(db, word)) is not None:
            if time.monotonic() >= deadline:
                if strict:
                    raise GenerationBusy()
                break
            time.sleep(1)

    return {word: find_by_word(db, word) for word in normalized}


def ensure_word_questions(db, word: str, difficulty: str) -> list:
    """Stored questions for one word, generated with Gemini only if missing."""
    normalized = normalize_word(word)
    return ensure_words_questions(db, [normalized], difficulty)[normalized]


def refill(db, difficulty: str, topic: Optional[str] = None, words: int = REFILL_WORDS) -> int:
    """Generate a fresh batch for random rounds. Returns the number of questions saved.

    Words are new words (not in the bank yet), optionally from one topic.
    Returns 0 without calling Gemini when another request is already refilling.
    """
    topic = normalize_topic(topic)
    lock_key = f"refill:{difficulty}:{topic or 'any'}"
    if not acquire_lock(db, lock_key):
        return 0
    try:
        known = set(db.questions.distinct("normalizedWord"))
        sample = set(random.sample(sorted(known), min(150, len(known))))
        _spend_gemini_call()
        suggested = gemini_service.suggest_words(difficulty, words, exclude=sample, topic=topic)
        fresh = [w for w in suggested if w not in known]
        if not fresh:
            return 0
        _spend_gemini_call(calls_needed(len(fresh), len(SUPPORTED_TYPES)))
        result = gemini_service.generate_questions(fresh, difficulty)
        for doc in result.questions:
            doc["topic"] = topic
        inserted, duplicates = save_questions(db, result.questions)
        logger.info("Refill level=%s topic=%s words=%d saved=%d duplicates=%d rejected=%d",
                    difficulty, topic, len(fresh), inserted, duplicates, len(result.rejected))
        return inserted
    finally:
        release_lock(db, lock_key)