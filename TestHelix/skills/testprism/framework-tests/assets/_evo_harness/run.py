"""Python 3.6+; no downloads, task expectations, or source mutations."""
import json
import os
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))
from _evo_harness.runtime import MODE, MODULE_PATHS
for relative in reversed(MODULE_PATHS):
    directory = (BASE.parent / relative).resolve()
    if BASE.parent.resolve() not in directory.parents:
        raise RuntimeError('Module path escapes the public project.')
    sys.path.insert(0, str(directory))

control = sys.argv[1:] == ['--control']
if sys.argv[1:] and not control:
    raise SystemExit('Only --control is supported; normal execution discovers cases/test_*.py.')

if MODE == 'django':
    # This isolated harness deliberately does not inherit a developer's external settings.
    os.environ['DJANGO_SETTINGS_MODULE'] = 'evo_harness_app.settings'
    import django
    django.setup()
    from django.test.runner import DiscoverRunner

    class NonemptyRunner(DiscoverRunner):
        def build_suite(self, *args, **kwargs):
            suite = super(NonemptyRunner, self).build_suite(*args, **kwargs)
            if not suite.countTestCases():
                raise RuntimeError('No task tests found: add cases/test_*.py; bootstrap control is not regression coverage.')
            return suite

    labels = ['_evo_harness.control'] if control else [str(BASE / 'cases')]
    failed = NonemptyRunner(verbosity=2, interactive=False).run_tests(labels)
else:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName('_evo_harness.control') if control else loader.discover(str(BASE / 'cases'))
    if not suite.countTestCases():
        raise SystemExit('No task tests found: add cases/test_*.py; bootstrap control is not regression coverage.')
    failed = not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()

if control and not failed:
    print('TESTHELIX_HARNESS_CONTROL=' + json.dumps({'passed': True, 'mode': MODE}))
sys.exit(1 if failed else 0)
