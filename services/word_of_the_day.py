"""Word of the day.

Picks one word per date from the words already in MongoDB. No Gemini call is made
and nothing about the user is stored: the same date always gives the same word.

Only words that were stored before the date starts are candidates, so the word
does not change during the day when new words are added to the bank.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import date, datetime, timezone
from typing import Optional

from services import question_bank as bank
from services.gemini_service import SUPPORTED_TYPES

_cache: dict = {}
_cache_lock = threading.Lock()
_MAX_CACHED_DAYS = 5


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _candidates(db, day: date) -> list:
    """Words first stored before `day` starts (all words when there are none)."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    firsts = list(db.questions.aggregate([{"$group": {"_id": "$normalizedWord", "first": {"$min": "$createdAt"}}}]))
    older = sorted(f["_id"] for f in firsts if f.get("first") is not None and _aware(f["first"]) < start)
    return older or sorted(f["_id"] for f in firsts)


def word_of_the_day(db, day: date) -> Optional[dict]:
    """{"word", "definition", "example", "difficulty", "date"} or None when the bank is empty."""
    cache_key = (id(db), day)
    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]

    words = _candidates(db, day)
    if not words:
        return None
    index = int(hashlib.sha256(day.isoformat().encode("utf-8")).hexdigest(), 16) % len(words)
    word = words[index]

    docs = bank.find_by_word(db, word)
    if not docs:
        return None
    docs.sort(key=lambda d: SUPPORTED_TYPES.index(d["questionType"]) if d["questionType"] in SUPPORTED_TYPES else 99)
    doc = docs[0]
    target = doc["explanation"]["target"]
    result = {
        "word": word,
        "definition": target["definition"],
        "example": target["example"],
        "difficulty": doc["difficulty"],
        "date": day.isoformat(),
    }

    with _cache_lock:
        if len(_cache) >= _MAX_CACHED_DAYS:
            _cache.clear()
        _cache[cache_key] = result
    return result