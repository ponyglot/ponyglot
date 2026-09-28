"""django-parler: translations in a separate table, one row per language (`master` FK)."""

from django.apps import apps

from ponyglot.adapters import ModelAdapter


def _is_parler_model(model):
    meta = getattr(model, "_parler_meta", None)
    return meta is not None and not model._meta.abstract and not model._meta.proxy


class ParlerAdapter(ModelAdapter):
    name = "parler"

    def models(self):
        return [model for model in apps.get_models() if _is_parler_model(model)]

    def translated_field_names(self, model):
        return list(model._parler_meta.get_all_fields())

    def _model_field(self, model, name):
        translations_model = model._parler_meta.get_model_by_field(name)
        return translations_model._meta.get_field(name)

    def watched_models(self):
        watched = {}
        for model in self.synced_models():
            watched[model] = lambda instance: instance
            for meta in model._parler_meta:
                watched[meta.model] = lambda translation: translation.master
        return watched

    def _translation(self, obj, field_name, language):
        meta = obj._parler_meta._get_extension_by_field(field_name)
        return meta.model._default_manager.filter(master=obj, language_code=language).first()

    def source_value(self, obj, field_name):
        from ponyglot.conf import get_config

        return self.target_value(obj, field_name, get_config().source_language)

    def target_value(self, obj, field_name, language):
        translation = self._translation(obj, field_name, language)
        return getattr(translation, field_name, None) if translation else None

    def target_languages_with_content(self, obj):
        languages = set()
        for meta in obj._parler_meta:
            languages.update(obj.get_available_languages(related_name=meta.rel_name))
        return sorted(languages)

    def label(self, obj):
        from parler.utils.context import switch_language

        from ponyglot.conf import get_config

        with switch_language(obj, get_config().source_language):
            return super().label(obj)

    def slug_taken(self, obj, field_name, language, slug):
        meta = obj._parler_meta._get_extension_by_field(field_name)
        return (
            meta.model._default_manager.filter(language_code=language, **{field_name: slug})
            .exclude(master=obj)
            .exists()
        )

    def write(self, obj, language, values):
        obj.set_current_language(language)
        for field_name, text in values.items():
            setattr(obj, field_name, text)
        obj.save()
