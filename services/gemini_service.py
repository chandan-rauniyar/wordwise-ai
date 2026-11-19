"""Gemini service for WordWise AI (Phase 3).

Gemini only *creates vocabulary content* in bulk. This module:

* asks Gemini for a batch of questions (several words x several question types),
* validates every question on its own, and
* returns documents that are ready to be saved by the MongoDB layer (Phase 4).

It never touches MongoDB and never stores anything. Invalid questions are
dropped and reported; if nothing valid comes back, an error is raised.

Question types
--------------
SYNONYM, ANTONYM   pick the similar / opposite word
FILL_BLANK         pick the word that fits a sentence
MEANING            pick the correct definition of a word
ODD_ONE_OUT        pick the word that does not belong with three related words
WORD_FORM          pick the right form of a word for a sentence (admire / admiration)
SPELLING           pick the correct spelling
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_MODEL = "gemini-3.8-flash"  # same model as test_gemini.py; override with GEMINI_MODEL
CONTENT_VERSION = 2                 # 1 = synonym/antonym/fill-blank only, 2 = all question types

DIFFICULTIES = ("EASY", "MEDIUM", "HARD")
SUPPORTED_TYPES = ("SYNONYM", "ANTONYM", "FILL_BLANK", "MEANING", "ODD_ONE_OUT", "WORD_FORM", "SPELLING")

# Topics a user can pick for mixed rounds: key -> (label shown in the UI, description for Gemini)
TOPICS = {
    "business": ("Business and work", "business, finance, management and the workplace"),
    "academic": ("Exams and academic", "academic and exam vocabulary (IELTS, TOEFL, GRE style)"),
    "feelings": ("Feelings and character", "emotions, personality and relationships"),
    "travel": ("Travel and places", "travel, transport, places and culture"),
    "science": ("Science and nature", "science, nature, the environment and animals"),
    "technology": ("Technology and media", "technology, the internet, media and communication"),
    "health": ("Health and body", "health, medicine, fitness and the human body"),
    "society": ("Society and law", "society, law, government, politics and ethics"),
}

DISTRACTORS_PER_QUESTION = 3  # maximum stored per question
MIN_DISTRACTORS = {"MEANING": 2, "WORD_FORM": 1, "SPELLING": 2}  # all other types need exactly 3
MAX_WORDS_PER_BATCH = 12
MAX_QUESTIONS_PER_CALL = 14     # one Gemini call covers at most this many questions
MAX_QUESTIONS_PER_BATCH = 100
MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

BLANK = "___"
SEPARATE_SENTENCE_TYPES = ("FILL_BLANK", "WORD_FORM")  # question text is a sentence with a blank

_WORD_RE = re.compile(r"^[a-z]+(?:[-'][a-z]+)*$")
_MIN_WORD_LEN, _MAX_WORD_LEN = 2, 30

_sleep = time.sleep  # tests replace this so retries do not wait


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class GeminiError(Exception):
    """Base class. `user_message` is safe to show in the UI."""

    user_message = "Something went wrong while creating questions. Please try again."

    def __init__(self, message: str = "", *, user_message: Optional[str] = None):
        super().__init__(message or self.user_message)
        if user_message:
            self.user_message = user_message


class GeminiConfigError(GeminiError):
    user_message = "The question service is not configured correctly."


class GeminiAPIError(GeminiError):
    """The API or network failed (quota, outage, bad key, wrong model...)."""

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 retryable: bool = False, user_message: Optional[str] = None):
        super().__init__(message, user_message=user_message)
        self.status_code = status_code
        self.retryable = retryable


class GeminiInvalidResponse(GeminiError):
    """Gemini answered, but nothing usable came back."""

    user_message = "Could not create valid questions right now. Please try again."

    def __init__(self, message: str, *, rejected: Optional[list] = None):
        super().__init__(message)
        self.rejected = rejected or []


class QuestionRejected(Exception):
    """One generated question failed validation (internal)."""


# --------------------------------------------------------------------------
# Schemas sent to Gemini (structure only; real validation is in Python)
# --------------------------------------------------------------------------

class WordInfo(BaseModel):
    word: str
    definition: str
    example: str


class GeneratedQuestion(BaseModel):
    target_word: str
    question_type: str
    sentence: str  # sentence with ___ (FILL_BLANK, WORD_FORM) or theme (ODD_ONE_OUT); "" otherwise
    target: WordInfo
    correct: WordInfo
    distractors: list[WordInfo]


class GeneratedBatch(BaseModel):
    questions: list[GeneratedQuestion]


class SuggestedWords(BaseModel):
    words: list[str]


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------

@dataclass
class GenerationResult:
    questions: list[dict] = field(default_factory=list)   # valid, ready for MongoDB
    rejected: list[dict] = field(default_factory=list)    # {"word", "type", "reason"}
    missing: list[tuple] = field(default_factory=list)    # (word, type) with no valid question
    model: str = ""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def normalize_word(text: Any) -> str:
    """Normalise a word for searching and storing. Raises ValueError if invalid."""
    if not isinstance(text, str):
        raise ValueError("Please enter a single English word.")
    cleaned = unicodedata.normalize("NFKC", text).replace("’", "'").strip().lower()
    if not (_MIN_WORD_LEN <= len(cleaned) <= _MAX_WORD_LEN) or not _WORD_RE.match(cleaned):
        raise ValueError("Please enter a single English word (letters only).")
    return cleaned


def normalize_topic(value: Any) -> Optional[str]:
    """None / '' / 'general' mean 'any topic'. Raises ValueError for an unknown topic."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Choose a topic from the list.")
    key = value.strip().lower()
    if key in ("", "general", "any"):
        return None
    if key not in TOPICS:
        raise ValueError("Choose a topic from the list.")
    return key


def make_unique_key(normalized_word: str, question_type: str, correct: str, sentence: str = "") -> str:
    """Key for the unique index in MongoDB; prevents duplicate questions."""
    key = f"{normalized_word}:{question_type}:{correct}"
    if question_type in SEPARATE_SENTENCE_TYPES:
        digest = hashlib.sha1(" ".join(sentence.lower().split()).encode("utf-8")).hexdigest()[:10]
        key += f":{digest}"
    return key


def calls_needed(word_count: int, type_count: int) -> int:
    """How many Gemini calls generate_questions() makes for this many words and types."""
    per_call = max(1, MAX_QUESTIONS_PER_CALL // max(1, type_count))
    return -(-word_count // per_call)


def _clean_text(value: Any, label: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise QuestionRejected(f"{label} is missing")
    text = " ".join(value.split())
    if len(text) < 3:
        raise QuestionRejected(f"{label} is empty or too short")
    if len(text) > max_len:
        raise QuestionRejected(f"{label} is too long")
    return text


def _clean_word(value: Any, label: str) -> str:
    try:
        return normalize_word(value)
    except ValueError:
        raise QuestionRejected(f"{label} is not a single valid word: {value!r}")


def _word_info(info: WordInfo, label: str, need_example: bool = True) -> dict:
    example = _clean_text(info.example, f"{label} example", 300) if need_example else " ".join((info.example or "").split())[:300]
    return {
        "word": _clean_word(info.word, f"{label} word"),
        "definition": _clean_text(info.definition, f"{label} definition", 250),
        "example": example,
    }


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


def _edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _blank_sentence(value: Any, options: list) -> str:
    """Clean a sentence that has exactly one blank and does not give any option away."""
    sentence = " ".join((value or "").split())
    sentence = re.sub(r"_{2,}", BLANK, sentence)
    if sentence.count(BLANK) != 1:
        raise QuestionRejected("sentence must contain exactly one blank")
    if not 15 <= len(sentence) <= 200:
        raise QuestionRejected("sentence has an unsuitable length")
    for option in options:
        if _contains_word(sentence, option):
            raise QuestionRejected(f"sentence already contains the option {option!r}")
    return sentence


# --------------------------------------------------------------------------
# Validation of one generated question
# --------------------------------------------------------------------------

def validate_question(raw: GeneratedQuestion, *, requested_words: set, requested_types: tuple,
                      difficulty: str, model: str) -> dict:
    """Return a MongoDB-ready document or raise QuestionRejected."""
    word = _clean_word(raw.target_word, "target_word")
    if word not in requested_words:
        raise QuestionRejected("word was not requested")

    qtype = (raw.question_type or "").strip().upper()
    if qtype not in requested_types:
        raise QuestionRejected(f"question type {raw.question_type!r} was not requested")

    target = _word_info(raw.target, "target")
    correct = _word_info(raw.correct, "correct answer")
    distractors = [_word_info(d, f"distractor {i + 1}", need_example=(qtype != "SPELLING"))
                   for i, d in enumerate(raw.distractors or [])]

    if target["word"] != word:
        raise QuestionRejected("target word does not match the requested word")
    needed = MIN_DISTRACTORS.get(qtype, DISTRACTORS_PER_QUESTION)
    if len(distractors) < needed:
        raise QuestionRejected(f"needs {needed} distractors, got {len(distractors)}")
    distractors = distractors[:DISTRACTORS_PER_QUESTION]

    options = [correct["word"]] + [d["word"] for d in distractors]
    if len(set(options)) != len(options):
        raise QuestionRejected("options are not distinct")

    sentence = ""
    correct_answer = correct["word"]
    option_texts = [d["word"] for d in distractors]

    if qtype in ("SYNONYM", "ANTONYM"):
        if correct["word"] == word or word in options:
            raise QuestionRejected("the target word appears among the options")
        relation = "similar" if qtype == "SYNONYM" else "different"
        question_text = (f"Which word is similar to {word}?" if qtype == "SYNONYM"
                         else f"Which word is the opposite of {word}?")

    elif qtype == "FILL_BLANK":
        if correct["word"] != word:
            raise QuestionRejected("fill-in-the-blank answer must be the requested word")
        sentence = _blank_sentence(raw.sentence, options)
        relation = "fits"
        question_text = sentence

    elif qtype == "WORD_FORM":
        if len(word) >= 4 and any(option[:3] != word[:3] for option in options):
            raise QuestionRejected("every option must be a form of the requested word")
        sentence = _blank_sentence(raw.sentence, options)
        relation = "fits"
        question_text = sentence

    elif qtype == "MEANING":
        if correct["word"] != word:
            raise QuestionRejected("the correct meaning must describe the requested word")
        meanings = [correct["definition"]] + [d["definition"] for d in distractors]
        if len({m.casefold() for m in meanings}) != len(meanings):
            raise QuestionRejected("meanings are not distinct")
        if any(_contains_word(m, word) for m in meanings):
            raise QuestionRejected("a meaning contains the requested word")
        relation = "means"
        question_text = f"What does {word} mean?"
        correct_answer = correct["definition"]
        option_texts = [d["definition"] for d in distractors]

    elif qtype == "ODD_ONE_OUT":
        if len(distractors) != 3 or word not in [d["word"] for d in distractors]:
            raise QuestionRejected("the requested word must be one of the three related words")
        sentence = _clean_text(raw.sentence, "theme", 80)
        relation = "odd"
        question_text = "Which word does not belong with the others?"

    else:  # SPELLING
        if correct["word"] != word:
            raise QuestionRejected("the correct spelling must be the requested word")
        if len(word) < 5:
            raise QuestionRejected("word is too short for a spelling question")
        for d in distractors:
            if d["word"][0] != word[0] or not 1 <= _edit_distance(word, d["word"]) <= 2:
                raise QuestionRejected(f"{d['word']!r} is not a plausible misspelling of {word!r}")
        if _contains_word(target["definition"], word):
            raise QuestionRejected("the definition contains the requested word")
        relation = "spelled"
        meaning = target["definition"].rstrip(". ")
        question_text = f"Which is the correct spelling of the word that means “{meaning}”?"

    explanation = {
        "target": target,
        "correct": {**correct, "relation": relation},
        "distractors": distractors,
    }
    if qtype == "ODD_ONE_OUT":
        explanation["theme"] = sentence

    return {
        "word": word,
        "normalizedWord": word,
        "questionType": qtype,
        "difficulty": difficulty,
        "questionText": question_text,
        "correctAnswer": correct_answer,
        "distractors": option_texts,
        "explanation": explanation,
        "uniqueKey": make_unique_key(word, qtype, correct["word"], sentence),
        "source": "GEMINI",
        "model": model,
        "contentVersion": CONTENT_VERSION,
    }


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You write vocabulary quiz content for learners of English, in the style of Google Word Coach. "
    "Be accurate: every definition and example must be correct and natural. "
    "Reply with JSON only, matching the schema."
)

_LEVELS = {
    "EASY": "common everyday words; distractors clearly different in meaning",
    "MEDIUM": "less common words (CEFR B1-B2); distractors plausible but clearly wrong",
    "HARD": "advanced words (CEFR C1-C2); distractors tempting, for example similar topic or form",
}

TYPE_RULES = {
    "SYNONYM": "- SYNONYM: correct is a true synonym of the target in its most common sense. distractors: exactly 3 real words of the same part of speech that are NOT synonyms or antonyms of the target.",
    "ANTONYM": "- ANTONYM: correct is a true antonym of the target. distractors: exactly 3 real words of the same part of speech that are NOT antonyms or synonyms of the target. If the word has no clear antonym, OMIT that question; do not invent one.",
    "FILL_BLANK": "- FILL_BLANK: correct is the target word exactly as written in the list. sentence: one natural sentence where only that word fits the blank. distractors: exactly 3 words of the same part of speech and grammatical form that are wrong in meaning for the sentence. The sentence must not contain the target word or any distractor.",
    "MEANING": "- MEANING: correct is the target word itself, with its definition. distractors: exactly 3 OTHER real words of the same part of speech (not synonyms of the target), each with its own correct definition, so that every definition is a believable wrong meaning for the target. Keep all four definitions short (under 15 words), in the same style and length, and never use the target word inside any definition.",
    "ODD_ONE_OUT": "- ODD_ONE_OUT: distractors: exactly 3 related words that share a meaning, and one of them MUST be the target word itself. correct: one word of the same part of speech that clearly does NOT belong with them. sentence: a short phrase (3 to 8 words) that completes 'The other words are all about ...', for example 'feeling happy'.",
    "WORD_FORM": "- WORD_FORM: use the word family of the target (for example admire: admires, admired, admiring, admiration, admirable). correct: the one form that fits the sentence grammatically. distractors: 1 to 3 other real forms with the same stem (the first three letters must match the target). sentence: one natural sentence with exactly one blank written as ___ where only the correct form fits. The sentence must not contain any of the options. If the word has fewer than two forms, OMIT this question.",
    "SPELLING": "- SPELLING: correct is the target word, spelled correctly. distractors: exactly 3 believable misspellings (missing or doubled letters, swapped vowels) that start with the same letter, differ from the correct spelling by 1 or 2 letters, and are NOT real English words. For each distractor put a short note about the mistake in definition (for example 'Missing the double m') and leave example empty. The target definition must not contain the target word. OMIT words shorter than 5 letters.",
}


def build_generation_prompt(words: list, difficulty: str, question_types: tuple) -> str:
    rules = "\n".join(TYPE_RULES[t] for t in question_types)
    return f"""Create vocabulary quiz questions.

Words: {json.dumps(words)}
Question types: {json.dumps(list(question_types))}
Level: {difficulty} - {_LEVELS[difficulty]}

Make ONE question for EVERY word and EVERY requested type.

Fields for every question:
- target_word: the word from the list, lowercase.
- question_type: exactly one of the requested types.
- target: the target word with a short dictionary-style definition and one natural example sentence.
- correct: the correct answer (see the rules) with a definition and an example sentence.
- distractors: the wrong options (see the rules), each with a definition and an example sentence.
- sentence: used by some types (see the rules); otherwise "".

Rules by type:
{rules}

General rules:
- If a word in the list is not a real English word, OMIT all of its questions.
- Every "word" field is ONE lowercase English word (letters, hyphen or apostrophe only).
- All options in one question must be different from each other.
- Definitions: short, plain English, no leading capital or trailing full stop needed. Examples: one sentence that uses the word.
"""


def build_suggest_prompt(difficulty: str, count: int, exclude: list, topic: Optional[str] = None) -> str:
    excluded = json.dumps(sorted(exclude)[:150])
    topic_line = f"\nTopic: {TOPICS[topic][1]}. Every word must belong to this topic." if topic else ""
    return f"""Suggest {count} English vocabulary words for learners at this level: {difficulty} - {_LEVELS[difficulty]}.{topic_line}
Choose useful words that have clear synonyms. One lowercase word each, no phrases, no proper nouns, no duplicates.
Do not suggest any of these words: {excluded}"""


# --------------------------------------------------------------------------
# Gemini client and calls
# --------------------------------------------------------------------------

def get_model_name() -> str:
    load_dotenv()
    return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def get_client():
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiConfigError("GEMINI_API_KEY is missing. Check your .env file.")
    return genai.Client(api_key=api_key)


def _api_error_to_exception(exc: genai_errors.APIError) -> GeminiAPIError:
    code = getattr(exc, "code", None)
    retryable = code in RETRYABLE_STATUS
    if code == 429:
        msg = "The question service is busy or its quota is used up. Please try again in a few minutes."
    elif code in (401, 403):
        msg = "The question service rejected the API key. Check GEMINI_API_KEY."
    elif code == 404:
        msg = "The Gemini model was not found. Check GEMINI_MODEL."
    elif code is not None and code >= 500:
        msg = "The question service is temporarily unavailable. Please try again."
    else:
        msg = "The question service could not process the request."
    return GeminiAPIError(f"Gemini API error {code}", status_code=code, retryable=retryable, user_message=msg)


def _backoff(attempt: int) -> float:
    return min(2 ** attempt, 20) + random.uniform(0, 0.5)


def _generate_json(client, model: str, prompt: str, schema: type) -> Any:
    """Call Gemini and return the parsed pydantic object, with retries."""
    load_dotenv()
    timeout_ms = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "90")) * 1000
    max_tokens = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "24576"))
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.7,
        max_output_tokens=max_tokens,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )

    last_error: Exception = GeminiInvalidResponse("no attempt was made")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
            text = getattr(response, "text", None)
            if not text or not text.strip():
                raise GeminiInvalidResponse("Gemini returned an empty response")
            try:
                return schema.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise GeminiInvalidResponse(f"Gemini response did not match the expected JSON: {exc}") from exc
        except genai_errors.APIError as exc:
            last_error = _api_error_to_exception(exc)
            if not last_error.retryable:
                raise last_error from exc
        except httpx.TransportError as exc:
            last_error = GeminiAPIError(
                f"Network error: {type(exc).__name__}", retryable=True,
                user_message="Could not reach the question service. Check the connection and try again.")
        except GeminiInvalidResponse as exc:
            last_error = exc

        logger.warning("Gemini attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, last_error)
        if attempt < MAX_ATTEMPTS:
            _sleep(_backoff(attempt))

    raise last_error


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def generate_questions(words: list, difficulty: str = "EASY", question_types: tuple = SUPPORTED_TYPES,
                       *, client=None, model: Optional[str] = None) -> GenerationResult:
    """Generate and validate a batch of questions (several words x several types).

    Large batches are split into several Gemini calls (at most MAX_QUESTIONS_PER_CALL
    questions each) so the answers are never cut off. Returns valid questions plus a
    report of rejected/missing ones; if some calls fail but others work, the valid
    questions are returned.

    Raises ValueError for bad input, GeminiConfigError / GeminiAPIError when the
    service cannot be used, and GeminiInvalidResponse when nothing valid came back.
    """
    difficulty = (difficulty or "").upper()
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"difficulty must be one of {DIFFICULTIES}")
    question_types = tuple(dict.fromkeys(t.upper() for t in question_types))
    if not question_types or any(t not in SUPPORTED_TYPES for t in question_types):
        raise ValueError(f"question types must be a subset of {SUPPORTED_TYPES}")
    normalized = list(dict.fromkeys(normalize_word(w) for w in words))
    if not normalized:
        raise ValueError("at least one word is required")
    if len(normalized) > MAX_WORDS_PER_BATCH or len(normalized) * len(question_types) > MAX_QUESTIONS_PER_BATCH:
        raise ValueError("batch is too large")

    client = client or get_client()
    model = model or get_model_name()
    per_call = max(1, MAX_QUESTIONS_PER_CALL // len(question_types))
    chunks = [normalized[i:i + per_call] for i in range(0, len(normalized), per_call)]

    result = GenerationResult(model=model)
    seen_keys: set = set()
    last_error: Optional[GeminiError] = None

    for chunk in chunks:
        try:
            batch = _generate_json(client, model, build_generation_prompt(chunk, difficulty, question_types),
                                   GeneratedBatch)
        except GeminiInvalidResponse as exc:
            last_error = exc
            continue
        except GeminiAPIError as exc:  # quota, outage, bad key: more calls will not help
            last_error = exc
            break

        requested_words = set(chunk)
        for raw in batch.questions:
            try:
                doc = validate_question(raw, requested_words=requested_words, requested_types=question_types,
                                        difficulty=difficulty, model=model)
            except QuestionRejected as exc:
                result.rejected.append({"word": raw.target_word, "type": raw.question_type, "reason": str(exc)})
                logger.warning("Rejected generated question (%s/%s): %s", raw.target_word, raw.question_type, exc)
                continue
            if doc["uniqueKey"] in seen_keys:
                result.rejected.append({"word": doc["word"], "type": doc["questionType"], "reason": "duplicate in batch"})
                continue
            seen_keys.add(doc["uniqueKey"])
            result.questions.append(doc)

    covered = {(q["word"], q["questionType"]) for q in result.questions}
    result.missing = [(w, t) for w in normalized for t in question_types if (w, t) not in covered]

    if not result.questions:
        if last_error is not None and not result.rejected:
            raise last_error
        raise GeminiInvalidResponse("Gemini returned no valid questions", rejected=result.rejected)
    if last_error is not None:
        logger.warning("Some Gemini calls failed, returning partial results: %s", last_error)
    return result


def suggest_words(difficulty: str, count: int = 5, exclude: Optional[set] = None,
                  *, topic: Optional[str] = None, client=None, model: Optional[str] = None) -> list:
    """Ask Gemini for new words (used for random rounds). Returns clean, unique words."""
    difficulty = (difficulty or "").upper()
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"difficulty must be one of {DIFFICULTIES}")
    topic = normalize_topic(topic)
    count = max(1, min(int(count), MAX_WORDS_PER_BATCH))
    exclude = {normalize_word(w) for w in (exclude or set())}

    client = client or get_client()
    model = model or get_model_name()
    suggested = _generate_json(client, model, build_suggest_prompt(difficulty, count + 3, sorted(exclude), topic),
                               SuggestedWords)

    words: list = []
    for item in suggested.words:
        try:
            word = normalize_word(item)
        except ValueError:
            continue
        if word not in exclude and word not in words:
            words.append(word)
    if not words:
        raise GeminiInvalidResponse("Gemini suggested no usable words")
    return words[:count]