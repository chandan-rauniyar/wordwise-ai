from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/round/start", views.start_round, name="start_round"),
    path("api/answer", views.answer, name="answer"),
    path("api/word-of-the-day", views.word_of_the_day, name="word_of_the_day"),
]