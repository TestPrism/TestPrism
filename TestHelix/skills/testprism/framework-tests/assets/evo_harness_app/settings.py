SECRET_KEY = 'generic-test-harness-only'
from _evo_harness.runtime import PROJECT_APPS
INSTALLED_APPS = ['evo_harness_app'] + [app for app in PROJECT_APPS if app != 'evo_harness_app']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
MIGRATION_MODULES = {'evo_harness_app': None}
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'
USE_TZ = False
