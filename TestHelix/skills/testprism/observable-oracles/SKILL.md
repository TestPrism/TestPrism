---
name: observable-oracles
description: "Translate contracts into independent behavioral or structural evidence."
metadata:
  components: [generation, review]
---

For each requirement identify input → observable → expected-value basis → positive control. Derive expectations from public examples, small-input calculations or documented invariants before fitting a repair. A passing self-written repair validates execution, not its own assumptions. Prefer a known supported path or independently derived anchor to another implementation of the same formula; check that it applies to the task's semantics and repository version.

Cover required boundaries and interactions while accepting permitted implementations. Exact counts, ordering, representation and compatibility details need public support. Check the relevant repository convention before constraining a behavior left open by the task. Each control should detect a concrete vacuous solution or broken observation channel; a mock or cache must not bypass the target.

Keep original public reproductions and use bounded deterministic inputs with observable postconditions. For instrumentation, metamorphic checks, CLI observations or alternative APIs, load references/observation-controls.md when needed. For a disputed or coupled numerical oracle, load references/independent-controls.md.
