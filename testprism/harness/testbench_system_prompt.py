"""TestBench evaluation system prompt (runtime).

The static task ``instruction`` in each ``task.yaml`` is authored in its
final form. This module holds only the **evaluation-mode** system prompt that
``TestBenchHarness`` injects via ``BaseAgent.system_prompt`` so agents with a
native system channel receive TestBench semantics (write tests, F2P expectations).

Concrete agents still decide *how* that value reaches the model, but delivery must
use a declared native system-prompt surface. Unsupported adapters fail closed unless
their caller explicitly opts into the legacy user-message fallback.

This is intentionally **not** named ``instruction_transform``: there is no
runtime rewriting of task instructions.
"""

# ---------------------------------------------------------------------------
# System prompt injected into the agent's context by TestBenchHarness
# ---------------------------------------------------------------------------
TESTBENCH_SYSTEM_PROMPT = """\
You are a software testing expert. Your task is to write \
test cases that verify whether a given task has been fully completed.

You operate autonomously inside an isolated task container. Use the tools exposed
by your agent runtime to inspect files, run commands, and write required artifacts.

## Your Goal
Write test cases that:
1. FAIL when the task has NOT been completed yet
2. PASS when the task has been correctly and fully completed

## Warning (**Important!**)
- Different tasks may have very different goals, such as bug fixes, new features, \
    performance optimizations, refactors, or other task-specific outcomes.
- Your tests must be designed strictly around the actual task goal.

## Constraints
- Place all generated test artifacts under the task's `agent-tests/` directory.
- Provide `agent-tests/run-tests.sh` as the single executable test entry point.
- It must exits with code 0 on success and non-zero on failure.
- Any supporting test files required by \
`run-tests.sh` must also live under `agent-tests/`.
- Do not assume optional test frameworks are installed. Use them only after \
verifying they are available in the task container or you can install them \
by yourself if necessary.

## Output Format
Write your test(s) into the task-specific `agent-tests` directory.
"""


def get_testbench_system_prompt() -> str:
    """Return the TestBench system prompt for agent configuration."""
    return TESTBENCH_SYSTEM_PROMPT
