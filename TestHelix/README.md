# TestHelix

TestHelix is an execution-guided test-generation agent for TestPrism. The
TypeScript implementation turns a public task description into an executable
`agent-tests/` submission, runs it in an isolated workspace, reviews the
evidence, and integrates with the official TestPrism scorer.

## Requirements

- Node.js 22.19 or newer
- Docker for benchmark tasks
- An OpenAI Responses, OpenAI-compatible, or Anthropic model endpoint
- A Python environment compatible with the selected TestPrism release

## Quick Start

Install and build the agent:

```bash
npm ci
npm run build
```

Create a local environment file, fill in the endpoint and model, and load it
into the current shell. Secrets are read only from environment variables.

```bash
cp .env.example .env.local
set -a
. ./.env.local
set +a
```

Run the included local fixture:

```bash
node dist/cli.js run \
  --task examples/prime/task.json \
  --config configs/testprism.quickstart.json \
  --out .runs/prime
```

`runtime.kind=local` is intended only for trusted fixtures. Use the official
Docker runtime for benchmark tasks.

## Configuration

The default configuration uses the OpenAI Responses protocol and reads:

| Variable | Purpose |
| --- | --- |
| `TESTHELIX_MODEL` | Model identifier |
| `TESTHELIX_BASE_URL` | API base URL |
| `TESTHELIX_API_KEY` | API credential |
| `GITHUB_TOKEN` | Optional token for higher GitHub search limits |

Use `configs/default.json` for an OpenAI-compatible Chat Completions endpoint,
`configs/openai-responses.example.json` for Responses, or
`configs/anthropic.example.json` for Anthropic. Model credentials must not be
written into configuration files.

## TestPrism

Keep task data, patch sets, Docker state, and generated results outside the
TestHelix source directory. When this directory is inside a TestPrism checkout,
the benchmark is `..`; for a standalone checkout, substitute its location:

```bash
node dist/cli.js doctor --bench ..
node dist/cli.js inventory \
  --bench .. \
  --out .runs/inventory.json
```

The native adapter is available at
`integrations.testprism.agent:TestHelixAgent`. See
[TESTPRISM_QUICKSTART.md](TESTPRISM_QUICKSTART.md) for task preparation and
official scoring instructions.

Only `agent-tests/` is exported to TestPrism. Internal `validated` and
`provisional` states are not official passes; only an official result with
`is_resolved: true` counts as successful.

## Repository Layout

```text
src/                    TypeScript agent and runtime
tests/                  Unit and contract tests
skills/testprism/      Reusable test-generation skills
integrations/testprism Official harness adapter
configs/                Credential-free runtime examples
configs/experiments/    Reproduction and ablation presets
examples/prime/         Minimal local example
scripts/                Reproduction and validation utilities
```

Internal development notes, legacy implementations, local benchmark checkouts,
credentials, and generated runs are excluded by `.gitignore`.
The bundled skills are the fixed pre-evolution baseline. Example configurations
start with empty guidance, lessons, and skill routes; learned variants and
development results are not included in this artifact.

## Validation

```bash
npm run check
npm run build
python -B tests/testprism_integration_contract.py
```

Commands that inspect or clean a run:

```bash
node dist/cli.js report \
  --results /path/to/official-results.json \
  --bench ..
node dist/cli.js cleanup --out .runs/prime
```

Task containers use `network=none` by default. Host-side `web_search` and
`github_search` are read-only research tools and are not scoring evidence.
