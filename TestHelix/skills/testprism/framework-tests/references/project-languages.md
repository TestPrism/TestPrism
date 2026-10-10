# Parsers, analyzers and document tooling

Determine whether the requested contract concerns acceptance, a parsed structure, diagnostics, source locations, rendering or configuration precedence. A successful parse does not verify the requested tree, and exact diagnostic text is too strong unless the contract requires it. Preserve meaningful encoding, line-ending and locale inputs in a small fixture.

When testing pytest itself or a plugin, inspect the local fixture/hook setup. pytest discovers parent-directory `conftest.py` files and registered plugins, so an exported test in a different directory may not inherit a repository fixture. Reuse an existing isolated runner fixture where available; confirm the child invocation collected and executed the intended test. [pytest plugin discovery](https://docs.pytest.org/en/stable/how-to/writing_plugins.html).

Mypy has data-driven cases under `test-data` and documents focused `runtests.py`/pytest invocations. Read the checkout's case format and suite adapter before treating a fixture file as an executable Python test. A minimal public API or CLI invocation can be simpler if it verifies the same contract. [Mypy contributor guide](https://github.com/python/mypy/blob/master/CONTRIBUTING.md).

For lint, markup, glob, date, locale and schema libraries, inspect one neighboring positive and negative example. Distinguish syntax validity from semantic normalization, full-string matching from substring matching, and parser output from presentation only when relevant to the task. Keep valid nearby behavior as a control; do not invent extra rejected inputs. For Sphinx, identify the local builder/extension fixture and observe its generated artifact or diagnostics rather than merely importing the extension.

Use the project's installed runner and explicit test selection. pytest can target paths and node IDs; invoking `python -m pytest` also places the current directory on the import path, which matters when verifying the intended checkout is imported. [pytest invocation guide](https://docs.pytest.org/en/stable/how-to/usage.html).
