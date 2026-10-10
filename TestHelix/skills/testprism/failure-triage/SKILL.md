---
name: failure-triage
description: "Separate harness errors, behavioral failures, flaky evidence and unsupported assertions."
metadata:
  components: [preparation, repair]
---

Read the host exception/frame summary first and trace the first cause. A missing required public API may legitimately fail on baseline; broken generated fixtures, timeouts and missing executables need setup repair. Keep justified assertions.

After a source edit, run the same case and compare the symptom. If unchanged, verify the loaded module and actual call path before another edit; do not repeat broad discovery or unchanged commands. Passing your repair supports a hypothesis, not full correctness. Independent repairs are not ground truth.

Record the changed symptom, evidence artifact and next unresolved question. For nested commands, fixture errors or nondeterminism, load references/failure-localization.md when resource access is available.
