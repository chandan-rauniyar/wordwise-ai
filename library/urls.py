from django.urls import path, re_path

from . import views

app_name = "library"

# Mounted at /library/ by config/urls.py
urlpatterns = [
    # pages that read
    path("", views.word_list, name="word_list"),                                              # 1
    path("topics/", views.topic_list, name="topic_list"),                                     # 2
    path("topic/<slug:slug>/", views.topic_detail, name="topic_detail"),                      # 3  slug converter
    path("word/<slug:slug>/", views.word_detail, name="word_detail"),                         # 4  slug converter
    path("id/<int:pk>/", views.word_by_id, name="word_by_id"),                                # 5  int converter
    path("difficulty/<str:level>/", views.words_by_difficulty, name="words_by_difficulty"),   # 6  str converter
    re_path(r"^added/(?P<year>[0-9]{4})/$", views.words_by_year, name="words_by_year"),       # 7  re_path
    # create, update, delete (CRUD)
    path("words/add/", views.word_create, name="word_create"),                                # 8
    path("word/<slug:slug>/edit/", views.word_update, name="word_update"),                    # 9
    path("word/<slug:slug>/delete/", views.word_delete, name="word_delete"),                  # 10
    path("word/<slug:slug>/examples/add/", views.example_add, name="example_add"),            # 11
    path("example/<int:pk>/delete/", views.example_delete, name="example_delete"),            # 12
    # JSON
    path("api/words/", views.api_words, name="api_words"),                                    # 13
    path("api/words/<slug:slug>/", views.api_word_detail, name="api_word_detail"),            # 14
]
