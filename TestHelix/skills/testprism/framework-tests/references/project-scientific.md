# Scientific and symbolic contracts

Preserve the public mathematical object when designing the oracle. Separate exact equality, approximate numerical agreement, representation and physical equivalence. Resolve only assumptions that affect the stated behavior.

SymPy documents both its `bin/test` runner and pytest, with targeted module/test selection. Inspect the checkout's existing invocation and equality conventions first. For algebraic results, do not assume printed form, structural equality and mathematical equality are interchangeable; choose the relation required by the public task and justify any domain assumptions from its inputs. [SymPy test guide](https://docs.sympy.org/latest/contributing/new-contributors-guide/writing-tests.html).

Astropy's tests use pytest and package-local tests. Optional remote-data tests are normally skipped unless enabled, and compiled extension changes may require rebuilding the installed checkout. Prefer a small local fixture available in the task environment. A network or extension import failure is setup evidence. [Astropy test guide](https://docs.astropy.org/en/latest/development/testguide.html).

For units, time, coordinates and chemistry, inspect the public constructor and one existing conversion test before inventing expected values. Record the units, scale/frame and relevant equivalencies actually used. Derive an expected value independently from a documented relation or a hand-checkable input; a round trip through the same faulty conversion is insufficient by itself. Consult local examples for Pint, pint-pandas, ucumvert and OpenFF rather than borrowing Astropy-specific APIs.

Keras documents selectable backends and pytest-based tests. Verify the already configured backend and local test helpers before constructing a model; do not force a new backend merely because current docs show it. [Keras contributor guide](https://github.com/keras-team/keras/blob/master/CONTRIBUTING.md).

For Flax, Transformers, quantum and optimization libraries, resolve the task's execution model from neighboring tests: eager versus traced, symbolic versus evaluated, stateful versus explicit state, exact versus sampled. Use a tiny existing-supported instance. Backend availability, precision settings and randomness are observed environmental facts, not assumptions or reasons to relax the public contract.
