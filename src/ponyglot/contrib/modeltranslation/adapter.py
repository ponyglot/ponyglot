"""django-modeltranslation: one column per language (`title_de`).

A new language needs a schema migration, so only languages modeltranslation knows are
writable; every unit says so (`writable_languages`). Empty target columns are left `NULL`/empty
as they are, so modeltranslation's fallbacks keep working until a suggestion is applied.
"""

from ponyglot.adapters import ModelAdapter
from ponyglot.conf import get_config


class ModeltranslationAdapter(ModelAdapter):
    name = "modeltranslation"

    def models(self):
        from modeltranslation.translator import translator

        return [
            model
            for model in translator.get_registered_models(abstract=False)
            if not model._meta.proxy
        ]

    def translated_field_names(self, model):
        from modeltranslation.translator import translator

        return list(translator.get_options_for_model(model).fields)

    def watched_models(self):
        return {model: lambda instance: instance for model in self.synced_models()}

    def _column(self, field_name, language):
        from modeltranslation.utils import build_localized_fieldname

        return build_localized_fieldname(field_name, language)

    def source_value(self, obj, field_name):
        return self.target_value(obj, field_name, get_config().source_language)

    def target_value(self, obj, field_name, language):
        return getattr(obj, self._column(field_name, language), None)

    def writable_languages(self):
        from modeltranslation import settings as mt_settings

        available = set(mt_settings.AVAILABLE_LANGUAGES)
        return [language for language in get_config().languages if language in available]

    def unit_writable_languages(self):
        source = get_config().source_language
        return [language for language in self.writable_languages() if language != source]

    def target_languages_with_content(self, obj):
        return [
            language
            for language in self.writable_languages()
            if any(self.target_value(obj, name, language) for name in self.fields(type(obj)))
        ]

    def slug_taken(self, obj, field_name, language, slug):
        column = self._column(field_name, language)
        return type(obj)._default_manager.filter(**{column: slug}).exclude(pk=obj.pk).exists()

    def write(self, obj, language, values):
        columns = []
        for field_name, text in values.items():
            column = self._column(field_name, language)
            setattr(obj, column, text)
            columns.append(column)
        obj.save(update_fields=columns)
