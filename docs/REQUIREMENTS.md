# WordWise AI — Requirements and System Design (v2)

Version 2 replaces the first draft. Changes: bulk question generation, a refill algorithm, more question types, and **no user data in the database** (progress lives in the browser).

## 1. Overview

WordWise AI is a vocabulary practice web app inspired by Google Word Coach. The user enters a word, or starts without one, and plays short rounds of multiple-choice questions. After each round they see a score and an explanation for every question.

**Core idea:** Gemini *creates content in bulk*; MongoDB *stores it*; Django *serves quizzes from MongoDB*. Gemini is never called while a user is waiting on a question, unless the bank has run dry.

**Stack:** Python, Django, MongoDB with PyMongo (no Django ORM for quiz data), Google Gemini API (`google-genai`), HTML/CSS/JS, python-dotenv. No login.

## 2. Privacy rule (most important)

- The database stores **only vocabulary content**: words, questions, answers, explanations.
- The database stores **nothing about users**: no accounts, no sessions, no answers, no scores, no browser IDs.
- Everything personal lives **in the user's own browser** (`localStorage`):
  - `seenQuestionIds` — questions already shown (capped, for example the last 500).
  - `wrongQuestions` — `{questionId, wrongCount, lastWrongAt}` for revision.
  - `totalScore` and round history, if wanted.
- The browser sends these IDs to the server when it asks for a round. The server does not save them.
- Clearing browser data resets progress. This is accepted for version 1.

## 3. User experience

### Home page
- Optional word input (`admire`).
- "Start quiz" without a word gives a random-word round.
- Easy / Medium / Hard selector (used for random rounds and for generation).

### Round
- 5 questions, one at a time, two answer buttons ("A or B") and a Skip link.
- After an answer: show correct or wrong, then Next.
- The correct answer is never sent to the browser before the answer is submitted.

### Results page
- Score, `x/5`, and a ✓ / ✗ list.
- Each row expands to show the "Learn why" explanation (section 5).
- "Next round" button.

## 4. Question types

Wording in the UI says *similar / opposite*. These are the same as SYNONYM / ANTONYM, so only one stored type exists for each.

| Type | Example | Version |
|---|---|---|
| `SYNONYM` | Which word is similar to *admire*? esteem / enroll | 1 |
| `ANTONYM` | Which word is the opposite of *admire*? despise / desire | 1 |
| `FILL_BLANK` | "She ___ her teacher's patience." admired / enrolled | 1 |
| `MEANING` | What does *admire* mean? (two definitions) | 2 |
| `ODD_ONE_OUT` | Which does not belong? (4 options) | 2 |
| `WORD_FORM` | Choose the correct form: *admiration / admirable* | 2 |
| `CONFUSED_WORDS` | affect / effect, principal / principle | 2 |
| `IDIOM` / `PHRASAL_VERB` | meaning of "break the ice" | 3 |
| `ANALOGY` | calm : agitated :: admire : ? | 3 |

All version 1 types use **word options**, so one schema covers them. New types are added by extending the type list and the prompt rules, with no change to the collections.

## 5. Question document

Collection `questions`. One document is one playable question.

```json
{
  "_id": "ObjectId",
  "word": "admire",
  "normalizedWord": "admire",
  "questionType": "ANTONYM",
  "difficulty": "EASY",
  "questionText": "Which word is the opposite of admire?",
  "correctAnswer": "despise",
  "distractors": ["desire", "reveal", "mention"],
  "explanation": {
    "target":     { "word": "admire",  "definition": "...", "example": "..." },
    "correct":    { "word": "despise", "relation": "different", "definition": "...", "example": "..." },
    "distractors": [
      { "word": "desire", "definition": "...", "example": "..." }
    ]
  },
  "uniqueKey": "admire:ANTONYM:despise",
  "source": "GEMINI",
  "model": "gemini-...",
  "contentVersion": 1,
  "createdAt": "...",
  "updatedAt": "..."
}
```

- Three distractors are stored. Version 1 shows **one** (chosen at random) for the two-choice layout. More options can be turned on later without regenerating content.
- Options are built and shuffled on the server at quiz time.
- The results page shows the "Learn why" block: definition of the target word, how the correct word relates (similar/different), and the definition of the distractor that was shown. Each has an example sentence.
- For `FILL_BLANK`, `questionText` is the sentence with `___`, and `correctAnswer` is the missing word.
- Unique index on `uniqueKey`. Extra indexes: `(normalizedWord)`, `(difficulty, questionType)`.

Collection `generation_locks` (small, with a TTL index) stops two requests generating the same word at the same time.

There is no `quiz_sessions` or `user_progress` collection.

## 6. Gemini integration

Gemini only generates and explains vocabulary. It never manages quizzes.

### Bulk generation
- One call generates **a batch**: for example 5 words × 4 types = 20 questions (batch size is configurable, 10 to 20).
- **Word-based request:** the user's word is sent first, along with a few related words so the batch is a full 20.
- **Random request:** the app asks Gemini to suggest new words for the difficulty, excluding words already in the database, then generates the questions.

### Output and validation
Gemini returns JSON using a response schema. The app then validates **every question** and saves only the valid ones:

- Correct structure, with all fields present and non-empty.
- Distractors are distinct from each other, from the correct answer, and from the target word.
- The word and type match what was requested. Difficulty is set by the app, not trusted from Gemini.
- Question text for synonym and antonym is generated by the app from a template.
- `FILL_BLANK` sentences contain exactly one `___`.
- Duplicates are removed through `uniqueKey`.

If only some questions are invalid, the valid ones are saved and the rest are dropped and logged. If nothing is valid, or Gemini or the network fails, the user gets a clear error and nothing is saved.

Model name comes from `GEMINI_MODEL` in `.env`. The API key stays in `.env`. Retries use backoff for quota (429) and server (5xx) errors.

## 7. Question selection and refill algorithm

```
start_round(word?, difficulty, seenIds[], wrongIds[]):

  1. REVISION   pick up to 2 questions from wrongIds
                (only those not answered correctly since; never the first question)
  2. WORD MODE  if word given:
                  take stored questions for normalizedWord not in seenIds
                  if fewer than 2 exist -> generate batch for that word
                                           (lock -> generate -> validate -> save)
  3. FILL       fill the round to 5 with random questions where
                  difficulty matches and _id not in seenIds,
                  mixing types and avoiding two questions of the same word in a row
  4. REFILL     if unseen questions for this difficulty < LOW_WATERMARK (for example 15)
                  generate a new batch in the background, for the next round
                if unseen < 5 right now, generate synchronously, then fill
  5. ORDER      shuffle, but place revision questions after at least 2 other questions
```

Rules:
- A question the user got wrong comes back in a **later round** (and may repeat in the same round only after other questions). It stays in `wrongQuestions` until answered correctly once; after that it is removed.
- Revision questions are tagged "Review" in the UI.
- If `seenIds` covers most of the bank, a refill is triggered. If Gemini is unavailable, already-seen questions are reused instead of failing.

## 8. Answer handling (stateless)

- `POST /api/round/start` returns 5 question objects (without correct answers) and a **signed round token** (Django signing) with the question IDs.
- `POST /api/answer` takes `{roundToken, questionId, chosen}`. The server looks up the question, compares the answer, and returns `{correct, points, explanation}`.
- Skip is a normal call that returns the explanation with 0 points.
- Points are 100 per correct answer. The browser keeps the running total. There is no leaderboard, so trusting the browser's total is harmless. Each answer is still checked on the server.
- No data is saved.

## 9. Security and reliability

- API key outside source control. Secrets are never logged.
- Validate all input on the backend (word length and characters, difficulty, ID format, at most 500 IDs).
- Rate-limit the endpoints that can call Gemini (per IP, with a global daily cap).
- Handle MongoDB and Gemini failures gracefully.
- Only validated content is stored.

## 10. Project structure

```
wordwise-ai/
  config/                  Django settings, root urls
  quiz/                    views, urls, templates/quiz/
  services/
    gemini_service.py      Gemini calls + validation       (Phase 3)
    question_bank.py       MongoDB reads/writes, reuse       (Phase 4)
    round_builder.py       selection + refill algorithm      (Phase 5)
  database.py              PyMongo connection
  static/css, static/js
  docs/REQUIREMENTS.md
  .env, .env.example, requirements.txt
```

## 11. Development plan

| Phase | Work |
|---|---|
| 1 | Setup (done: Python, Django, MongoDB, Gemini tested) |
| 2 | Requirements and data design (this document) |
| 3 | Gemini service: bulk generation and validation |
| 4 | Question bank: save, reuse, unique index, lock, no repeat Gemini call |
| 5 | Round builder and answer checking |
| 6 | Frontend: home, question, feedback, results, localStorage progress |
| 7 | Integration tests, including failures |
| 8 | More question types, deployment |

## 12. Version 1 acceptance criteria

1. Home page accepts a word or starts a random quiz.
2. Existing questions are read from MongoDB; Gemini is called only when the bank is short.
3. A Gemini batch of 10 to 20 questions is validated and stored.
4. One question is shown at a time with two options and Skip.
5. Answers are checked by Django; the correct answer is never sent early.
6. Wrong questions come back in a later round, tracked in the browser only.
7. The results page shows the score and a "Learn why" explanation for each question.
8. A repeated request reuses stored content.
9. The database contains no user data.
10. No login is needed.