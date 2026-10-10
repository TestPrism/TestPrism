"""Tests of import isolation, not benchmark classes or benchmark scoring."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("native_diagnostic", Path(__file__).resolve().parents[1] / "scripts/run_native_bridge_diagnostic.py")
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class LazyImportContract(unittest.TestCase):
    def test_holdout_requires_explicit_sample_split_and_cannot_mix_with_development(self):
        hard, dev = 'from-swe-bench/repo__repo-1', 'from-swe-bench/repo__repo-2'
        diagnostic.validate_task_split(dev, {hard}, set(), 'development')
        diagnostic.validate_task_split(hard, {hard}, set(), 'hardest500')
        for key, existing, split in [(hard, set(), 'development'), (dev, set(), 'hardest500'),
                                     (hard, {hard}, 'hardest500'), ('../escape', set(), 'hardest500'),
                                     (dev, set(), 'unknown')]:
            with self.subTest(key=key, split=split), self.assertRaises(ValueError):
                diagnostic.validate_task_split(key, {hard}, existing, split)

    def test_frozen_agent_checkout_preserves_adapter_identity_and_cannot_select_external_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned, external = root / 'owned', root / 'external'
            frozen = owned / 'frozen'
            for base in [owned, frozen]:
                files = base / 'integrations/testprism'
                files.mkdir(parents=True)
                for name in ['agent.py', 'runner.py', '__init__.py']:
                    (files / name).write_text('# same public adapter\n')
            with patch.object(diagnostic, 'PROJECT', owned):
                self.assertEqual(diagnostic.resolve_agent_project(frozen, external), frozen)
                with self.assertRaisesRegex(ValueError, 'outside the external'):
                    diagnostic.resolve_agent_project(external, external)
                with self.assertRaisesRegex(ValueError, 'outside the external'):
                    diagnostic.resolve_agent_project(frozen, frozen)
                (frozen / 'integrations/testprism/runner.py').write_text('# changed adapter\n')
                with self.assertRaisesRegex(ValueError, 'Frozen adapter differs'):
                    diagnostic.resolve_agent_project(frozen, external)

    def test_optional_missing_export_does_not_block_available_native_export(self):
        source = 'from pathlib import Path as NativePath\nfrom definitely_missing_optional_agent import Missing\n__all__ = ["NativePath", "Missing"]\n'
        namespace = {}
        exec(diagnostic.lazy_initializer(source), namespace)
        self.assertNotIn("NativePath", namespace)
        self.assertEqual(namespace["__all__"], ["NativePath", "Missing"])
        self.assertIs(namespace["__getattr__"]("NativePath"), Path)
        self.assertIs(namespace["NativePath"], Path)
        with self.assertRaises(ModuleNotFoundError):
            namespace["__getattr__"]("Missing")
        with self.assertRaises(AttributeError):
            namespace["__getattr__"]("not_exported")

    def test_unknown_side_effects_and_unresolved_exports_fail_closed(self):
        for source in ('print("side effect")', 'from pathlib import *',
                       'from pathlib import Path\n__all__ = ["Missing"]',
                       'from .base_agent import BaseAgent\n__all__ = ["BaseAgent"]'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                diagnostic.lazy_initializer(source)

    def test_runtime_copy_keeps_implementations_and_external_files_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bench, out = root / "external", root / "owned"
            out.mkdir()
            for package in diagnostic.LAZY_PACKAGES:
                target = bench / "testprism" / package
                target.mkdir(parents=True)
                (target / "__init__.py").write_text('from pathlib import Path\n__all__ = ["Path"]\n')
                (target / "implementation.py").write_text('def unchanged(): return 42\n')
            originals = {p.relative_to(bench): p.read_bytes() for p in bench.rglob("*") if p.is_file()}
            original_path = sys.path[:]
            try:
                runtime = diagnostic.copy_runtime(bench, out)
            finally:
                sys.path[:] = original_path
            self.assertEqual(originals, {p.relative_to(bench): p.read_bytes() for p in bench.rglob("*") if p.is_file()})
            for package in diagnostic.LAZY_PACKAGES:
                self.assertEqual((runtime / "testprism" / package / "implementation.py").read_bytes(),
                                 (bench / "testprism" / package / "implementation.py").read_bytes())
                self.assertNotEqual((runtime / "testprism" / package / "__init__.py").read_bytes(),
                                    (bench / "testprism" / package / "__init__.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
