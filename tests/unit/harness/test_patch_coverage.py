import json

import pytest

from testprism.harness.patch_coverage import (
    calculate_patch_coverage,
    parse_unified_diff,
)

PATCH = """\
diff --git a/pkg/old.py b/pkg/new.py
similarity index 80%
rename from pkg/old.py
rename to pkg/new.py
--- a/pkg/old.py
+++ b/pkg/new.py
@@ -10,4 +10,5 @@
-old_a()
-old_b()
+new_a()
+new_b()
+extra()
 context()
-removed()
diff --git a/pkg/added.py b/pkg/added.py
new file mode 100644
--- /dev/null
+++ b/pkg/added.py
@@ -0,0 +1,2 @@
+# comment
+run()
"""


def test_parse_unified_diff_tracks_both_sides_and_change_kinds():
    changes = parse_unified_diff(PATCH)

    renamed, added = changes.files
    assert renamed.old_path == "pkg/old.py"
    assert renamed.new_path == "pkg/new.py"
    assert renamed.modified_old_lines == {10, 11}
    assert renamed.modified_new_lines == {10, 11}
    assert renamed.added_lines == {12}
    assert renamed.deleted_lines == {13}
    assert renamed.pre_target_lines == {10, 11, 13}
    assert renamed.post_target_lines == {10, 11, 12}
    assert added.old_path is None
    assert added.added_lines == {1, 2}
    assert changes.added_line_count == 3
    assert changes.modified_line_count == 2
    assert changes.deleted_line_count == 1


def test_calculate_patch_coverage_uses_reference_targets_and_excludes_comments():
    changes = parse_unified_diff(PATCH)
    reference_baseline = {
        "files": {
            "pkg/old.py": {
                "executed_lines": [10, 13],
                "missing_lines": [11],
            }
        }
    }
    reference_gold = {
        "files": {
            "pkg/new.py": {
                "executed_lines": [10, 12],
                "missing_lines": [11],
            },
            "pkg/added.py": {
                "executed_lines": [2],
                "missing_lines": [],
            },
        }
    }

    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[reference_baseline],
        generated_gold_reports=[reference_gold],
        reference_baseline_reports=[reference_baseline],
        reference_gold_reports=[reference_gold],
        artifact_path="coverage/gold-patch.json",
    )

    assert result.status == "collected"
    assert result.executable_changed_line_count == 5
    assert result.covered_changed_line_count == 5
    assert result.untraceable_line_count == 1
    assert result.reference_unexecuted_line_count == 2
    assert result.non_statement_line_count == 1
    assert result.ratio == pytest.approx(1.0)
    assert result.artifact_path == "coverage/gold-patch.json"


def test_calculate_patch_coverage_merges_multiple_process_reports():
    changes = parse_unified_diff(
        """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-before()
+after()
"""
    )
    reference_baseline = {
        "files": {"a.py": {"executed_lines": [1], "missing_lines": []}}
    }
    reference_gold = {
        "files": {"a.py": {"executed_lines": [1], "missing_lines": []}}
    }
    generated_baseline = {
        "files": {"a.py": {"executed_lines": [], "missing_lines": [1]}}
    }
    post_a = {"files": {"a.py": {"executed_lines": [], "missing_lines": [1]}}}
    post_b = {"files": {"./a.py": {"executed_lines": [1], "missing_lines": []}}}

    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[generated_baseline],
        generated_gold_reports=[post_a, post_b],
        reference_baseline_reports=[reference_baseline],
        reference_gold_reports=[reference_gold],
    )

    assert result.executable_changed_line_count == 2
    assert result.covered_changed_line_count == 1
    assert result.ratio == 0.5


def test_calculate_patch_coverage_matches_installed_package_suffix():
    changes = parse_unified_diff(
        """diff --git a/django/core/validators.py b/django/core/validators.py
--- a/django/core/validators.py
+++ b/django/core/validators.py
@@ -94 +94 @@
-old_regex = 1
+new_regex = 1
"""
    )
    installed = (
        "/opt/conda/lib/python3.5/site-packages/Django.egg/django/core/validators.py"
    )
    report = {
        "files": {
            installed: {"executed_lines": [94], "missing_lines": []},
        }
    }

    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[report],
        generated_gold_reports=[report],
        reference_baseline_reports=[report],
        reference_gold_reports=[report],
    )

    assert result.status == "collected"
    assert result.covered_changed_line_count == 2
    assert result.ratio == 1.0


def test_calculate_patch_coverage_reports_non_executable_patch():
    changes = parse_unified_diff(
        """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-# old
+# new
"""
    )
    empty = {"files": {"a.py": {"executed_lines": [], "missing_lines": []}}}

    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[empty],
        generated_gold_reports=[empty],
        reference_baseline_reports=[empty],
        reference_gold_reports=[empty],
    )

    assert result.status == "no_changed_lines"
    assert result.ratio is None
    assert result.untraceable_line_count == 2


def test_calculate_patch_coverage_reports_unsupported_language():
    changes = parse_unified_diff(
        """diff --git a/app.rs b/app.rs
--- a/app.rs
+++ b/app.rs
@@ -1 +1 @@
-old();
+new();
"""
    )

    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[],
        generated_gold_reports=[],
        reference_baseline_reports=[],
        reference_gold_reports=[],
    )

    assert result.status == "unsupported"
    assert result.ratio is None


def test_calculate_patch_coverage_reports_missing_or_malformed_data(tmp_path):
    changes = parse_unified_diff(
        """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old()
+new()
"""
    )
    malformed = tmp_path / "coverage.json"
    malformed.write_text("not json")

    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[malformed],
        generated_gold_reports=[],
        reference_baseline_reports=[malformed],
        reference_gold_reports=[],
    )

    assert result.status == "failed"
    assert "invalid coverage report" in result.reason

    missing_lines = {"files": {"a.py": {"executed_lines": []}}}
    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[missing_lines],
        generated_gold_reports=[],
        reference_baseline_reports=[missing_lines],
        reference_gold_reports=[],
    )
    assert result.status == "failed"
    assert "lacks valid line lists" in result.reason


def test_report_file_input_and_empty_diff(tmp_path):
    changes = parse_unified_diff("")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"files": {}}))

    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[report],
        generated_gold_reports=[report],
        reference_baseline_reports=[report],
        reference_gold_reports=[report],
    )

    assert result.status == "no_changed_lines"
    assert result.reason == "gold patch has no changed source lines"


def test_malformed_hunk_raises_clear_error():
    with pytest.raises(ValueError, match="malformed unified diff hunk range"):
        parse_unified_diff(
            """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -x +1 @@
-old()
+new()
"""
        )


def test_generated_reports_cannot_enlarge_or_shrink_reference_denominator():
    changes = parse_unified_diff(
        """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-before()
+after()
"""
    )
    reference = {
        "files": {"a.py": {"executed_lines": [1], "missing_lines": []}}
    }
    generated_extra = {
        "files": {
            "a.py": {"executed_lines": [1, 99], "missing_lines": []},
            "unrelated.py": {"executed_lines": [1], "missing_lines": []},
        }
    }
    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[generated_extra],
        generated_gold_reports=[generated_extra],
        reference_baseline_reports=[reference],
        reference_gold_reports=[reference],
    )
    assert result.executable_changed_line_count == 2
    assert result.covered_changed_line_count == 2
    assert result.ratio == 1.0

    omitted = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[{"files": {}}],
        generated_gold_reports=[{"files": {}}],
        reference_baseline_reports=[reference],
        reference_gold_reports=[reference],
    )
    assert omitted.executable_changed_line_count == 2
    assert omitted.covered_changed_line_count == 0
    assert omitted.generated_unavailable_line_count == 2
    assert omitted.ratio == 0.0


def test_reference_unexecuted_and_non_statement_lines_are_classified():
    changes = parse_unified_diff(
        """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@
-old()
-comment()
-not_reported()
+new()
+comment2()
+not_reported2()
"""
    )
    reference = {
        "files": {
            "a.py": {"executed_lines": [1], "missing_lines": [2]},
        }
    }
    generated = {
        "files": {"a.py": {"executed_lines": [1, 2, 3], "missing_lines": []}}
    }
    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[generated],
        generated_gold_reports=[generated],
        reference_baseline_reports=[reference],
        reference_gold_reports=[reference],
    )
    assert result.executable_changed_line_count == 2
    assert result.covered_changed_line_count == 2
    assert result.reference_unexecuted_line_count == 2
    assert result.non_statement_line_count == 2
    assert len(result.line_classifications["reference_unexecuted"]) == 2
    assert len(result.line_classifications["non_statement"]) == 2


def test_explicit_report_inputs_require_all_relevant_suites():
    changes = parse_unified_diff(
        """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-before()
+after()
"""
    )
    report = {"files": {"a.py": {"executed_lines": [1], "missing_lines": []}}}
    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[report],
        generated_gold_reports=[report],
        reference_baseline_reports=[report],
    )
    assert result.status == "failed"
    assert "reference gold" in result.reason


def test_reference_report_missing_target_file_fails_explicitly():
    changes = parse_unified_diff(
        """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1 +1 @@
-before()
+after()
"""
    )
    empty = {"files": {}}
    generated = {
        "files": {"pkg/a.py": {"executed_lines": [1], "missing_lines": []}}
    }
    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[generated],
        generated_gold_reports=[generated],
        reference_baseline_reports=[empty],
        reference_gold_reports=[generated],
    )
    assert result.status == "failed"
    assert result.ratio is None
    assert result.reason == "reference baseline report missing target file 'pkg/a.py'"


def test_mixed_patch_scores_registered_targets_and_audits_unsupported_files():
    changes = parse_unified_diff(
        """diff --git a/pkg/code.py b/pkg/code.py
--- a/pkg/code.py
+++ b/pkg/code.py
@@ -1 +1 @@
-before()
+after()
diff --git a/native/tool.rs b/native/tool.rs
--- a/native/tool.rs
+++ b/native/tool.rs
@@ -1 +1 @@
-old()
+new()
"""
    )
    report = {
        "files": {"pkg/code.py": {"executed_lines": [1], "missing_lines": []}}
    }

    result = calculate_patch_coverage(
        changes,
        generated_baseline_reports=[report],
        generated_gold_reports=[report],
        reference_baseline_reports=[report],
        reference_gold_reports=[report],
    )

    assert result.status == "collected"
    assert result.language == "python"
    assert result.languages == ["python"]
    assert result.unsupported_changed_files == ["native/tool.rs"]
    assert result.executable_changed_line_count == 2
    assert result.covered_changed_line_count == 2
    assert result.ratio == 1.0
