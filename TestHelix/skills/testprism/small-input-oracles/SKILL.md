---
name: small-input-oracles
description: "Use bounded independent oracles, metamorphic checks and numeric controls."
metadata:
  components: [generation]
---

For algorithmic code, derive a simple exhaustive or brute-force oracle on small inputs independently of the implementation being tested. Choose a size bound before generating cases; seed sampled cases and print the failing input. Avoid transcribing the same optimized algorithm into the oracle.
Where exact outputs are nonunique, validate feasibility and objective value instead of one chosen representation. Metamorphic relations such as permutation, renaming, scaling or decomposition are useful only if justified by this contract; combine them with concrete anchors because a constant wrong function can satisfy relations.
For numerical work choose tolerances from precision and conditioning, check shapes and non-finite values explicitly, and use exact small cases when possible. Do not silently cast away dtype, order, multiplicity or overflow behavior that the contract requires.
