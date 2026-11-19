from django.contrib import admin
from django.db.models import Count

from .models import Example, Topic, Word


class ExampleInline(admin.TabularInline):
    """Edit the example sentences on the word's own admin page."""

    model = Example
    extra = 1


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "number_of_words")
    search_fields = ("name", "description")
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_words=Count("words"))

    @admin.display(description="Words", ordering="_words")
    def number_of_words(self, obj):
        return obj._words


@admin.register(Word)
class WordAdmin(admin.ModelAdmin):
    list_display = ("text", "topic", "part_of_speech", "difficulty", "created_at")
    list_filter = ("topic", "difficulty", "part_of_speech")
    search_fields = ("text", "definition")
    prepopulated_fields = {"slug": ("text",)}
    date_hierarchy = "created_at"
    inlines = [ExampleInline]


@admin.register(Example)
class ExampleAdmin(admin.ModelAdmin):
    list_display = ("sentence", "word", "created_at")
    search_fields = ("sentence", "word__text")
    list_select_related = ("word",)
