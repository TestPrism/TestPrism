---
name: control-evidence
description: Audit positive and preservation controls before rejecting a regression suite for an unsupported assertion.
metadata:
  components: [review]
---
Distinguish required regression assertions, controls that detect a vacuous solution or broken observation channel, and extra acceptance constraints. A task need not restate established public compatibility. Check relevant repository tests, docs and interfaces; baseline behavior alone may be the bug and is not sufficient authority.

For each disputed control, ask what incorrect solution it detects and what public evidence supports its expected outcome. Do not reject it solely because the task omits it. Equally, calling an assertion a control does not justify arbitrary ordering, exact counts or implementation coupling.

To block, describe a concrete compliant behavior the assertion would reject, explain why that behavior remains allowed given compatibility evidence, and cite the public basis. Record unresolved interpretations as suggestions. Do not invent evidence to complete the protocol, weaken valid assertions merely to appease review, or use hidden patches as authority.
