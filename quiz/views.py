"""Quiz pages and JSON API.

GET  /                  the single-page quiz app
POST /api/round/start   {word?, words?[], topic?, difficulty, seenIds[], wrongIds[]} -> a round of questions
POST /api/answer        {roundToken, questionId, chosen|null}      -> result + explanation
GET  /api/word-of-the-day?date=YYYY-MM-DD                          -> {word, definition, example}

The server stores nothing about the user. Progress lives in the browser.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone

from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from pymongo.errors import PyMongoError

from services import question_bank as bank
from services import round_builder as rounds
from services import word_of_the_day as wotd
from services.gemini_service import TOPICS, GeminiAPIError, GeminiError, GeminiInvalidResponse
from services.mongo import get_db

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 40_000
START_LIMIT_PER_MINUTE = 20
ANSWER_LIMIT_PER_MINUTE = 150
WOTD_LIMIT_PER_MINUTE = 60
MAX_LIST_ITEMS = 40


class BadRequest(Exception):
    pass


def _error(message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _read_json(request) -> dict:
    if len(request.body) > MAX_BODY_BYTES:
        raise BadRequest("Request is too large.")
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BadRequest("Request is not valid JSON.")
    if not isinstance(data, dict):
        raise BadRequest("Request must be a JSON object.")
    return data


def _rate_limited(request, scope: str, limit: int, window_seconds: int = 60) -> bool:
    ip = request.META.get("REMOTE_ADDR", "unknown")
    key = f"ratelimit:{scope}:{ip}"
    if cache.add(key, 1, window_seconds):
        return False
    try:
        return cache.incr(key) > limit
    except ValueError:  # key expired between add() and incr()
        cache.set(key, 1, window_seconds)
        return False


def _list_of_strings(value, name: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadRequest(f"{name} must be a list of strings.")
    return value


@ensure_csrf_cookie
@require_GET
def index(request):
    topics = [{"key": key, "label": label} for key, (label, _prompt) in TOPICS.items()]
    return render(request, "quiz/index.html", {"topics": topics})


def _requested_day(raw) -> date:
    """The user's local date (sent by the browser), limited to one day around the server date."""
    today = datetime.now(timezone.utc).date()
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return today
    return day if abs((day - today).days) <= 1 else today


@require_GET
def word_of_the_day(request):
    if _rate_limited(request, "wotd", WOTD_LIMIT_PER_MINUTE):
        return _error("Too many requests. Please wait a minute and try again.", 429)
    try:
        result = wotd.word_of_the_day(get_db(), _requested_day(request.GET.get("date")))
    except PyMongoError:
        logger.exception("Database error while choosing the word of the day")
        return _error("The question database is unavailable. Please try again soon.", 503)
    if result is None:
        return _error("There is no word of the day yet.", 404)
    return JsonResponse(result)


@require_POST
def start_round(request):
    if _rate_limited(request, "start", START_LIMIT_PER_MINUTE):
        return _error("Too many requests. Please wait a minute and try again.", 429)
    word = None
    try:
        data = _read_json(request)
        word = data.get("word")
        if word is not None and not isinstance(word, str):
            raise BadRequest("Word must be text.")
        difficulty = data.get("difficulty", "EASY")
        if not isinstance(difficulty, str):
            raise BadRequest("Difficulty must be text.")
        seen = _list_of_strings(data.get("seenIds"), "seenIds")
        wrong = _list_of_strings(data.get("wrongIds"), "wrongIds")
        topic = data.get("topic")
        if topic is not None and not isinstance(topic, str):
            raise BadRequest("Topic must be text.")
        word_list = _list_of_strings(data.get("words"), "words")
        if len(word_list) > MAX_LIST_ITEMS or any(len(w) > 40 for w in word_list):
            raise BadRequest("Your word list is too long.")

        payload = rounds.build_round(get_db(), word=word, difficulty=difficulty, seen_ids=seen,
                                     wrong_ids=wrong, topic=topic, words=word_list or None)
        return JsonResponse(payload)
    except BadRequest as exc:
        return _error(str(exc), 400)
    except ValueError as exc:
        return _error(str(exc), 400)
    except GeminiInvalidResponse:
        if word and word.strip():
            return _error("We could not build questions for that word. Check the spelling or try another word.", 422)
        return _error(GeminiInvalidResponse.user_message, 503)
    except GeminiAPIError as exc:
        return _error(exc.user_message, 429 if exc.status_code == 429 else 503)
    except GeminiError as exc:
        return _error(exc.user_message, 503)
    except (bank.GenerationBusy, bank.DailyLimitReached, rounds.NotEnoughQuestions) as exc:
        return _error(exc.user_message, 503)
    except PyMongoError:
        logger.exception("Database error while starting a round")
        return _error("The question database is unavailable. Please try again soon.", 503)
    except Exception:
        logger.exception("Unexpected error while starting a round")
        return _error("Something went wrong. Please try again.", 500)


@require_POST
def answer(request):
    if _rate_limited(request, "answer", ANSWER_LIMIT_PER_MINUTE):
        return _error("Too many requests. Please wait a minute and try again.", 429)
    try:
        data = _read_json(request)
        token, question_id = data.get("roundToken"), data.get("questionId")
        if not isinstance(token, str) or not isinstance(question_id, str):
            raise BadRequest("roundToken and questionId are required.")
        chosen = data.get("chosen")
        if chosen is not None and not isinstance(chosen, str):
            raise BadRequest("chosen must be text or null.")

        result = rounds.check_answer(get_db(), token=token, question_id=question_id, chosen=chosen)
        return JsonResponse(result)
    except BadRequest as exc:
        return _error(str(exc), 400)
    except (rounds.InvalidRound, rounds.InvalidAnswer) as exc:
        return _error(exc.user_message, 400)
    except PyMongoError:
        logger.exception("Database error while checking an answer")
        return _error("The question database is unavailable. Please try again soon.", 503)
    except Exception:
        logger.exception("Unexpected error while checking an answer")
        return _error("Something went wrong. Please try again.", 500)