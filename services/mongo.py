"""MongoDB connection for the quiz collections (PyMongo, no Django ORM).

Reads the connection settings from .env. Several common variable names are
accepted so it works with the .env you already use for test_mongodb.py:

    URI : MONGODB_URI | MONGO_URI | MONGODB_URL | MONGO_URL   (default mongodb://localhost:27017)
    DB  : MONGODB_DB_NAME | MONGODB_DB | MONGO_DB_NAME | MONGO_DB | DB_NAME   (default wordwise_ai)
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.database import Database

_URI_NAMES = ("MONGODB_URI", "MONGO_URI", "MONGODB_URL", "MONGO_URL")
_DB_NAMES = ("MONGODB_DB_NAME", "MONGODB_DB", "MONGO_DB_NAME", "MONGO_DB", "DB_NAME")
DEFAULT_DB_NAME = "wordwise_ai"

_lock = threading.Lock()
_client: Optional[MongoClient] = None
_indexes_ready = False


def _first_env(names, default=""):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def get_db() -> Database:
    """Return the application database (connects lazily, creates indexes once)."""
    global _client, _indexes_ready
    with _lock:
        if _client is None:
            load_dotenv()
            uri = _first_env(_URI_NAMES, "mongodb://localhost:27017")
            _client = MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
        db = _client[_first_env(_DB_NAMES, DEFAULT_DB_NAME)]
        if not _indexes_ready:
            ensure_indexes(db)
            _indexes_ready = True
    return db


def ensure_indexes(db: Database) -> None:
    """Create the indexes the app relies on (safe to call many times)."""
    questions = db["questions"]
    questions.create_index([("uniqueKey", ASCENDING)], unique=True, name="uniq_question")
    questions.create_index([("normalizedWord", ASCENDING)], name="by_word")
    questions.create_index([("difficulty", ASCENDING), ("questionType", ASCENDING)], name="by_level_type")
    questions.create_index([("topic", ASCENDING), ("difficulty", ASCENDING)], name="by_topic_level")
    # Locks expire by themselves, so a crashed request cannot block generation forever.
    db["generation_locks"].create_index([("expireAt", ASCENDING)], expireAfterSeconds=0, name="lock_ttl")