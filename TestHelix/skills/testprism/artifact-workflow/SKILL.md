---
name: artifact-workflow
description: "Close gaps between public requirements, executable tests and a replayable repair control."
metadata:
  components: [preparation, generation, repair]
---

Verify the selected runtime or built target executes this checkout with a small supported operation. If dependencies are missing, inspect existing project environments first. For setup questions, load_skill with id framework-tests and resource references/runtime-discovery.md.

Track the missing evidence: justified expectations, runnable tests, the baseline symptom, and the same tests passing with a repair. Choose actions that close those gaps. Save a small justified suite early with write_test_suite; a command exit alone does not prove its cases ran. Keep expectations independent of the repair. Keep agent-tests/ at the initial working directory even if public source is elsewhere. For command/state changes or external public-source edits, save an internal repair script inside the workspace, outside agent-tests/, and submit its replayScript path. External edits alone are not captured; tests observe required results.

Use feedback with existing artifacts and evidence: distinguish baseline regressions, setup failures and disputed assertions. Apply supported corrections, run the changed suite, then submit_candidate. When evidence identifies the change, do not restart discovery or rerun unchanged files. If feedback conflicts with the assertion or public contract, preserve the justified test and explain with evidence. Verify and submit before adding optional coverage.

For interrupted execution or artifact recovery, load references/artifact-recovery.md only when needed.
