---
name: framework-tests
description: "Discover the installed runtime and bootstrap framework tests without setup false failures."
metadata:
  components: [preparation, generation, repair]
---

Inspect a nearby working test and the configured runtime. For unfamiliar conventions, load references/project-context.md and only the relevant family; verify its hints against this checkout. Run a small supported operation with expected output. Finding an executable or receiving exit code zero is insufficient. Reuse the project runner and environment. For missing imports, load references/runtime-discovery.md before changing the test approach.

If a standalone Python/Django harness is needed, prepare_test_harness installs fixed assets and runs a setup control without installing dependencies or overwriting files. A passing control proves setup only. Repeated preparation reuses matching owned files and reruns the control; edited or unowned files remain untouched. Read the receipt before rebuilding.

Use django mode for ORM cases; unittest mode registers no Django apps or tables. Add public-task TestCase test_* methods in agent-tests/cases/test_*.py. For project models, load references/django-models.md to check app registration, inherited tables and isolated fixtures. Scope instrumentation after setup and before cleanup. Keep required resources in agent-tests/. Trace import errors, broken fixtures and zero collected tests to their first cause; a newly requested API may legitimately be absent on baseline.
