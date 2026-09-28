SECRET_KEY = "tests"
USE_I18N = True
USE_TZ = True
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("de", "German"), ("fr", "French")]
MODELTRANSLATION_LANGUAGES = ("en", "de")  # fr has no columns: not writable for these units
PARLER_LANGUAGES = {None: ({"code": "en"}, {"code": "de"}, {"code": "fr"})}
PARLER_ENABLE_CACHING = False  # the in-memory test database reuses primary keys

INSTALLED_APPS = [
    "modeltranslation",  # before django.contrib.admin
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "parler",
    "ponyglot",
    "ponyglot.contrib.parler",
    "ponyglot.contrib.modeltranslation",
    "tests.testapp",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "tests.urls"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
PONYGLOT = {
    "API_KEY": "pg_test",
    "API_URL": "https://api.example.test/v1",
    "EXCLUDE_FIELDS": ["testapp.article.internal_note"],
}
