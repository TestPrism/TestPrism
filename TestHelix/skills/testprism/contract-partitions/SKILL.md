---
name: contract-partitions
description: "Separate required equivalence classes from plausible near-miss behavior."
metadata:
  components: [generation]
---

Build a short table of public requirements, input classes, and justified observations. For each assertion ask which plausible incomplete implementation it distinguishes and which alternative correct implementations it must accept. Derive near misses from the contract, not hidden solutions or your own repair's structure.
Partition boundaries and interactions: empty/nonempty, single/multiple, repeated/distinct, valid/invalid only where specified. For parsers and serializers distinguish semantic equality from exact bytes; require ordering, whitespace or error wording only when the public contract does. A round trip alone can let two coupled mistakes cancel, so add independently known anchors.
Add positive controls for valid alternatives. Keep tests that reject different classes of mistakes; redundant examples that exercise the same path have low marginal value. Unsupported behavior remains an open question, not a stronger acceptance rule.
