# Rendering and component projects

Identify the observable layer named by the task: geometry, object state, serialized output, browser events or final pixels. Assert at that layer. A source-text match or a successful render command alone does not establish behavior.

For Matplotlib, inspect a neighboring test and the checkout's test configuration to reuse its backend, figure lifecycle and comparison helpers. Its upstream test guide documents ordinary and image-comparison tests; image comparison has additional dependencies and reference assets. Prefer a public geometric or object-state assertion when that is the stated contract, while retaining a rendered observation for an explicitly visual requirement. [Matplotlib test guide](https://matplotlib.org/stable/devel/testing.html).

For Svelte, consult local contributor guidance and package scripts: upstream documents test samples and separate suites, and some tests require a browser. Verify whether the checkout's entry point imports source or built output before relying on a source repair as a positive control. [Svelte contributor guide](https://github.com/sveltejs/svelte/blob/main/CONTRIBUTING.md).

For p5.js, follow the local version's harness. Current upstream documents Vitest with Mocha-compatible globals; older revisions can differ. Seeing `suite` and `test` does not identify the underlying runner. A browser-dependent drawing API needs a supported canvas/browser fixture, not an invented object with the same method names. [p5.js test guide](https://github.com/processing/p5.js/blob/main/contributor_docs/unit_testing.md).

For React/PDF/component monorepos, inspect the owning package's scripts and neighboring fixtures before choosing Node, DOM emulation or a real browser. Use the lockfile and package metadata to identify module format and runner. Await the documented completion event/update before observation, and clean up objects created by the test. PDF bytes, layout metrics and screenshots are different oracles; use the one supported by the task, including stable normalization only for fields outside its contract.
