"""Task-independent checks: interpreter, discovery, app registration and DB lifecycle."""
import unittest
from _evo_harness.runtime import MODE


if MODE == 'django':
    from django.test import TestCase
    from django.apps import apps
    from evo_harness_app.models import HarnessParent, HarnessChild

    class HarnessControl(TestCase):
        def test_registered_fixture_lifecycle(self):
            self.assertIs(apps.get_model('evo_harness_app', 'HarnessChild'), HarnessChild)
            parent = HarnessParent.objects.create(value=7)
            HarnessChild.objects.create(parent=parent)
            self.assertEqual(parent.harnesschild_set.count(), 1)
            parent.delete()
            self.assertEqual(HarnessChild.objects.count(), 0)

        def test_isolated_database(self):
            self.assertEqual(HarnessParent.objects.count(), 0)
else:
    class HarnessControl(unittest.TestCase):
        def test_interpreter_and_discovery(self):
            self.assertEqual(sum([2, 3]), 5)
