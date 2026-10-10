# TestPrism — anonymous research artifact

This repository is the anonymized code artifact accompanying the paper *TestPrism: Rethinking Test Evaluation Beyond a Single Reference*. It contains the TestPrism evaluation framework and **TestHelix**, the test-generation agent introduced in the paper. Benchmark tasks and multi-patch evaluation assets are distributed separately through anonymous Hugging Face repositories.

- Project page: <https://testprism.github.io/>
- Anonymous paper: <https://testprism.github.io/assets/testprism-paper.pdf>

The unfinished patch-generation producer is intentionally excluded. This release supports consuming frozen patch sets; it does not include the code or private generation traces used to produce them.

## Repository contents

- `testprism/`: benchmark CLI, agents, container runtime, parsers, evaluation harness, and the read-only multi-patch contract in `testprism/patches/`.
- `TestHelix/`: the paper's execution-guided test-generation agent, including source, fixed skills, benchmark adapter, configurations, and tests.
- `tests/`: framework tests, including the multi-patch contract tests.
- `scripts/validate_artifact.py`: offline structural, size, and anonymity checks for this code repository.

The following data is **not** stored in this repository:

- `verified_tasks/test`: the complete 2,000-task benchmark test split.
- `patch_sets`: the frozen 300-task multi-patch collection used in the paper, with ten candidates per task.
- `benchmark300.json`: the outcome-independent manifest selecting the 300-task multi-patch subset.

## Obtain the data

During anonymous review, download the two companion Hugging Face repositories:

- Verified tasks: [testprism-anonymous/testprism-verified-tasks](https://huggingface.co/datasets/testprism-anonymous/testprism-verified-tasks)
- Multi-patch sets: [testprism-anonymous/testprism-patch-sets](https://huggingface.co/datasets/testprism-anonymous/testprism-patch-sets)

Copy or symlink their contents into this checkout so that the layout is:

```text
TestPrism/
├── verified_tasks/
│   └── test/                 # exactly 2,000 task.yaml files
├── patch_sets/                  # exactly 300 manifest.json files
└── benchmark300.json
```

The 2,000-task split is the primary benchmark release. Multi-patch evaluation applies to the fixed 300-task subset named by `benchmark300.json`; a patch set is not expected for every task in the full split.

## Requirements

- Python 3.12 or later
- Node.js 22.19 or later (for TestHelix)
- [`uv`](https://docs.astral.sh/uv/)
- Docker with Compose support
- Credentials required by the selected coding agent

## Verify the code artifact

Before downloading data, run the code-only integrity and anonymity checks:

```bash
uv run python scripts/validate_artifact.py
```

Run the self-contained multi-patch contract tests without starting task containers:

```bash
uv run pytest tests/unit/patches tests/unit/harness/test_testbench_patch_set_v4.py
```

Some broader integration tests require Linux/Docker or development-only fixtures that are intentionally not part of this anonymous artifact.

Install, type-check, and build TestHelix:

```bash
cd TestHelix
npm ci
npm run typecheck
npm run build
cd ..
```

## Run an evaluation

After placing the downloaded data in the layout above, copy the environment template and add only the credentials required by your chosen agent. Do not commit `.env`.

For the complete 2,000-task benchmark:

```bash
cp .env.template .env
uv sync

uv run tb testbench run \
  --dataset-path verified_tasks/test \
  --agent codex \
  --model openai/<model-name> \
  --n-concurrent 1 \
  --no-cleanup
```

For multi-patch evaluation, use the same task split together with the downloaded patch assets and select task IDs from `benchmark300.json`:

```bash
uv run tb testbench run \
  --dataset-path verified_tasks/test \
  --patch-set-path patch_sets \
  --task-id <TASK_ID_FROM_BENCHMARK300> \
  --agent codex \
  --model openai/<model-name> \
  --n-concurrent 1 \
  --patch-concurrency 1 \
  --no-cleanup
```

Use `--task-id`, `--n-tasks`, and the concurrency flags to control evaluation scope and resource use. Each task builds or pulls its own Docker environment; a full run is compute- and storage-intensive.

Useful result commands:

```bash
uv run tb runs status --run-id <RUN_ID>
uv run tb runs summarize --run-id <RUN_ID>
uv run tb runs resume --run-id <RUN_ID>
```

## Run TestHelix

TestHelix reads credentials only from environment variables. Its complete setup and native TestPrism adapter instructions are in [`TestHelix/README.md`](TestHelix/README.md) and [`TestHelix/TESTPRISM_QUICKSTART.md`](TestHelix/TESTPRISM_QUICKSTART.md).

```bash
cd TestHelix
cp .env.example .env.local
# Fill TESTHELIX_MODEL, TESTHELIX_BASE_URL, and TESTHELIX_API_KEY.
set -a && . ./.env.local && set +a

node dist/cli.js doctor --bench ..
node dist/cli.js inventory --bench .. --out .runs/inventory.json
```

For official scoring, use the TestHelix native adapter documented in the quickstart. Only the generated `agent-tests/` directory crosses into TestPrism; reference solutions, patch labels, and multi-patch assets remain hidden from the agent.

## Multi-patch protocol

The agent writes one test suite. The harness first runs it against the baseline, then evaluates the same suite in independent clean environments against all ten candidates for that task. A strict solve requires detecting the baseline, accepting every oracle-valid candidate, rejecting every oracle-invalid candidate, and encountering no patch execution errors.

Patch candidates and labels are evaluation-side assets. They must not be exposed to the evaluated agent or copied into a task's build context.

## Scope, privacy, and anonymity

The public artifact is rebuilt with a fresh, single-commit Git history authored as `Anonymous <anonymous@invalid>`. Upstream development remotes, commit authors, local paths, generation attempt logs, credentials, data assets, paper sources, and development history are not included. Please report accidental disclosure privately through the paper's anonymous review channel rather than opening a public issue.

The framework is released under Apache 2.0; see `LICENSE`. Individual task assets in the companion data release may retain upstream notices and licenses.
