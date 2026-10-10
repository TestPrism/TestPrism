---
name: performance-controls
description: "Verify required efficiency without timing noise or changing semantics."
metadata:
  components: [generation, repair]
---

Establish functional equivalence first. Identify which resource or operation the public requirement constrains: repeated computation, data copies, queries, allocations, asymptotic growth or latency. Prefer direct observation of that resource when instrumentation can preserve behavior.
Exercise the path at sizes where the claimed difference appears, with bounded runtime and a small correctness oracle. Avoid tests that pass by skipping work, changing output precision, depending on warmed global state or caching one fixed input.
If timing is the actual contract, isolate setup, warm up both cases consistently, repeat bounded measurements, and use a justified margin large enough for environmental variance. A timeout or one unusually fast run is not a performance oracle. Do not invent a numerical speed threshold absent a defensible requirement.
