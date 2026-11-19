"""Fill the library with sample topics, words and example sentences.

    python manage.py load_sample_words

Safe to run again: rows that already exist are left as they are.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from library.models import Example, Topic, Word

# topic name -> (description, [(word, part of speech, difficulty, definition, example), ...])
SAMPLE = {
    "Business": (
        "Words for work, money and meetings.",
        [
            ("negotiate", "verb", "MEDIUM", "To discuss something in order to reach an agreement.", "They will negotiate the price of the contract on Monday."),
            ("revenue", "noun", "MEDIUM", "The money a company receives from selling goods or services.", "The company's revenue grew by ten percent this year."),
            ("deadline", "noun", "EASY", "The time or date by which something must be finished.", "The deadline for the report is Friday at noon."),
            ("delegate", "verb", "MEDIUM", "To give part of your work or power to another person.", "A good manager knows when to delegate tasks."),
        ],
    ),
    "Academic": (
        "Words used in study, writing and exams.",
        [
            ("hypothesis", "noun", "HARD", "An idea that is suggested as an explanation and has to be tested.", "Her hypothesis was that plants grow faster with more light."),
            ("analyse", "verb", "MEDIUM", "To study something in detail in order to understand it.", "Students must analyse the poem and explain its meaning."),
            ("coherent", "adjective", "HARD", "Clear, logical and easy to follow.", "He gave a coherent answer to a difficult question."),
        ],
    ),
    "Feelings": (
        "Words for emotions and character.",
        [
            ("resilient", "adjective", "MEDIUM", "Able to recover quickly after a difficulty.", "Children are often more resilient than adults expect."),
            ("reluctant", "adjective", "MEDIUM", "Not willing to do something.", "She was reluctant to speak in front of the class."),
            ("humble", "adjective", "EASY", "Not proud; not thinking you are better than others.", "Despite his success, he stayed humble."),
        ],
    ),
    "Travel": (
        "Words for trips, journeys and places.",
        [
            ("itinerary", "noun", "MEDIUM", "A detailed plan of a journey, with places and times.", "Our itinerary includes two days in Jaipur."),
            ("souvenir", "noun", "EASY", "An object you keep to remember a place you visited.", "I bought a small souvenir from the market."),
            ("excursion", "noun", "MEDIUM", "A short trip, usually made for pleasure by a group.", "The school arranged an excursion to the museum."),
        ],
    ),
    "Science": (
        "Words from laboratories and nature.",
        [
            ("experiment", "noun", "EASY", "A test done to learn something or to prove an idea.", "The experiment showed that the metal expands when heated."),
            ("ecosystem", "noun", "MEDIUM", "All the living things in an area and how they depend on each other.", "The lake is a fragile ecosystem."),
            ("catalyst", "noun", "HARD", "Something that causes a change or speeds up a reaction.", "The speech was a catalyst for political change."),
        ],
    ),
    "Technology": (
        "Words about computers, the internet and media.",
        [
            ("algorithm", "noun", "MEDIUM", "A set of steps that a computer follows to solve a problem.", "The search algorithm ranks pages by how useful they are."),
            ("encryption", "noun", "HARD", "Changing information into a code so that only some people can read it.", "Encryption keeps your messages private."),
            ("bandwidth", "noun", "MEDIUM", "The amount of data that can be sent over a connection in a given time.", "Streaming video needs a lot of bandwidth."),
        ],
    ),
    "Health": (
        "Words about the body and medicine.",
        [
            ("nutrition", "noun", "EASY", "The food a person needs to stay healthy.", "Good nutrition helps children grow."),
            ("chronic", "adjective", "HARD", "Lasting for a long time, or happening again and again.", "He has chronic back pain."),
            ("immunity", "noun", "MEDIUM", "The body's ability to resist a disease.", "The vaccine gives immunity against the virus."),
        ],
    ),
    "Society": (
        "Words about law, rights and community.",
        [
            ("legislation", "noun", "HARD", "Laws, or the process of making laws.", "New legislation will protect workers' rights."),
            ("tolerance", "noun", "MEDIUM", "Willingness to accept opinions and behaviour that differ from your own.", "A fair society values tolerance."),
            ("verdict", "noun", "MEDIUM", "The decision of a court about whether someone is guilty.", "The jury reached a verdict after two days."),
        ],
    ),
}


class Command(BaseCommand):
    help = "Load sample topics, words and examples into the word library."

    @transaction.atomic
    def handle(self, *args, **options):
        added_words = 0
        for topic_name, (description, words) in SAMPLE.items():
            topic, _ = Topic.objects.get_or_create(name=topic_name, defaults={"description": description})
            for text, part_of_speech, difficulty, definition, sentence in words:
                word, created = Word.objects.get_or_create(
                    text=text,
                    defaults={
                        "topic": topic,
                        "part_of_speech": part_of_speech,
                        "difficulty": difficulty,
                        "definition": definition,
                    },
                )
                if created:
                    added_words += 1
                    Example.objects.create(word=word, sentence=sentence)
        self.stdout.write(self.style.SUCCESS(
            f"Done. {added_words} new words added. Library now has "
            f"{Topic.objects.count()} topics, {Word.objects.count()} words and {Example.objects.count()} examples."
        ))
