# WordWise AI — setup and run (Phases 3–6)

## 1. Files

```
services/gemini_service.py   Gemini: bulk generation + validation
services/mongo.py            MongoDB connection + indexes
services/question_bank.py    save / reuse / locks / refill
services/round_builder.py    round selection (word / list / topic / mix), revision, answers
services/word_of_the_day.py  word of the day from the stored words
quiz/views.py, quiz/urls.py  pages and JSON API
quiz/templates/quiz/index.html
quiz/static/quiz/app.js, style.css
test_gemini_service.py, test_quiz_flow.py, test_features.py, test_gemini_live.py
```

`quiz/views.py` and `quiz/urls.py` replace the default files that `startapp` created.

## 2. Install

```
pip install -r requirements.txt
```

## 3. `.env`

```
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.8-flash
MONGODB_URI=mongodb://localhost:27017      # or your Atlas URI
MONGODB_DB_NAME=wordwise_ai
GEMINI_DAILY_CALL_LIMIT=300                # optional safety cap
```

`services/mongo.py` also accepts `MONGO_URI`, `MONGODB_URL`, `MONGODB_DB`, `MONGO_DB_NAME`. If your `database.py` already has the connection, edit `get_db()` in `services/mongo.py` to return it.

## 4. Django wiring

`config/settings.py`: `INSTALLED_APPS` must contain `"quiz"`, and `django.contrib.staticfiles` must be enabled (it is by default). Django's `db.sqlite3` is not used for quiz data.

`config/urls.py`:

```python
from django.urls import include, path

urlpatterns = [
    path("", include("quiz.urls")),
]
```

## 5. Run

```
py -m unittest test_gemini_service test_quiz_flow test_features -v   # offline, no key needed
py test_gemini_live.py                                  # real Gemini
py manage.py runserver                                  # open http://127.0.0.1:8000/
```

The first request creates the MongoDB indexes automatically.

## 6. Behaviour notes

- **Typed word:** up to 3 questions about that word (a random mix of its 7 types) come first, whatever the difficulty. If the word is not stored yet, Gemini creates all 7 types in one call. Difficulty applies to the rest of the round and to random rounds.
- **Random round:** unseen questions of the chosen difficulty (and topic), mixing words and question types. When fewer than 15 unseen remain, a background refill asks Gemini for 4 new words (about 28 questions). If the bank has fewer than 5 unseen right now, a smaller refill of 2 words runs before the round starts.
- **Revision:** a question that was answered wrongly or skipped is remembered in the browser and returns in a later round (up to 2 per round, never first). It leaves the list when answered correctly.
- **Privacy:** MongoDB stores only vocabulary questions and short-lived generation locks. Seen/wrong IDs and points live in the browser's `localStorage`.
- **Answers:** the correct answer is never sent before the answer is submitted. The round is described by a signed token (valid 2 hours) that also fixes which wrong option was shown.
- **Limits:** 20 round starts and 150 answers per minute per IP, and a daily Gemini call cap. These counters are per process; with several server processes, use a shared cache such as Redis.

## 7. Topics, word list, word of the day and audio

- **Topic:** a menu on the home page. The list of topics is `TOPICS` in `services/gemini_service.py`; add or rename entries there and the menu follows.
- **Word list:** "My word list" on the home page. Words are saved in the browser only. A new word costs one Gemini call the first time, then it is reused for everyone.
- **Word of the day:** picked from the words already stored, so an empty database shows no card. It changes at midnight in the visitor's own time zone (the browser sends its date; the server accepts one day either side of its own date).
- **Audio:** uses the browser's speech voices. Voice quality depends on the device. Chrome and Edge on Windows and Android have good English voices; if a browser has none, the speaker buttons are hidden.
- **Words from the first version:** the first time someone practises one, Gemini adds the missing question types (one call). Nothing else is needed.