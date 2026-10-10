---
name: stateful-sequences
description: "Test state transitions, cache invalidation, transactions and cleanup."
metadata:
  components: [preparation, generation, repair]
---

Represent the public behavior as operations and observable state transitions. Test short meaningful sequences, not just isolated calls: create/read/update/read, repeated action, rollback/retry, and independent instances where required. Reinitialize fixtures between sequences so test order cannot supply hidden state.
For caches, observe both returned values and the underlying work after relevant mutations. For transactions, check committed state and rollback state through supported interfaces. Framework registration and database constraints must be established before the behavioral assertion.
Observe required effects without requiring undocumented callback signatures, internal query text or a specific number of operations. Concurrency tests need a controlled schedule or synchronization point; sleeps are not synchronization. Ensure cleanup on both success and failure, without masking the original assertion.

For protocol or client/server contracts, load references/local-io.md through load_skill with this ID and resource path. It covers bounded loopback fixtures and independent observations without external services.
