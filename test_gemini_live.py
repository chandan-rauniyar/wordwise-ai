"""Live test: calls the real Gemini API (needs GEMINI_API_KEY in .env).

Usage (from the project root):
    python test_gemini_live.py                      # words: admire, belie
    python test_gemini_live.py startling upset      # your own words
    python test_gemini_live.py --suggest MEDIUM     # ask Gemini for new words
"""

import json
import sys

from services import gemini_service as gs


def main(args):
    try:
        if args and args[0] == "--suggest":
            difficulty = args[1].upper() if len(args) > 1 else "EASY"
            print(f"Suggested {difficulty} words:", gs.suggest_words(difficulty, 5))
            return 0

        words = args or ["admire", "belie"]
        print(f"Model: {gs.get_model_name()}")
        print(f"Generating for: {words} ...")
        result = gs.generate_questions(words, "EASY")
    except ValueError as exc:
        print("Bad input:", exc)
        return 1
    except gs.GeminiError as exc:
        print("Gemini error:", exc.user_message, f"({exc})")
        for item in getattr(exc, "rejected", []):
            print("  rejected:", item)
        return 1

    print(f"\nValid: {len(result.questions)}  Rejected: {len(result.rejected)}  Missing: {result.missing}")
    for item in result.rejected:
        print("  rejected:", item)

    for q in result.questions:
        print(f"\n[{q['questionType']}] {q['questionText']}")
        print(f"  correct    : {q['correctAnswer']}")
        print(f"  distractors: {', '.join(q['distractors'])}")

    print("\nFull document of the first question:")
    print(json.dumps(result.questions[0], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))