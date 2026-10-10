# TestPrism Integration

TestHelix exports only `agent-tests/`. The TestPrism checkout, task data,
Docker images, patch sets, and scoring outputs remain external to this
repository.

## 1. Prepare TestHelix

Install the Node.js dependencies, build the agent, and export model settings:

```bash
npm ci
npm run build
export TESTHELIX_API_KEY=your-key
export TESTHELIX_BASE_URL=https://api.openai.com/v1
export TESTHELIX_MODEL=your-model-id
```

When TestHelix is a folder inside TestPrism, the benchmark checkout is `..`.
For a standalone TestHelix checkout, substitute the TestPrism path below.
Verify the integration:

```bash
node dist/cli.js doctor --bench ..
node dist/cli.js inventory \
  --bench .. \
  --out .runs/inventory.json
```

## 2. Prepare a Public Task

A minimal descriptor for an already-running TestPrism container is:

```json
{
  "id": "source/public-task-id",
  "instruction": "Original public task instruction",
  "source": "testprism",
  "runtime": {
    "kind": "testprism",
    "container": "container-name",
    "root": "/testbed",
    "socket": "/tmp/testprism.sock"
  }
}
```

It can also be generated from a public task directory:

```bash
node dist/cli.js prepare-task \
  --bench .. \
  --key source/public-task-id \
  --container running-container-name \
  --out .runs/task.json
```

## 3. Run and Clean Up

```bash
node dist/cli.js run \
  --task .runs/task.json \
  --config configs/testprism.quickstart.json \
  --out .runs/testprism-run

node dist/cli.js cleanup --out .runs/testprism-run
```

The task container has no network access by default. Optional host-side search
does not modify the container and does not count as official evaluation
evidence.

## 4. Use the Native Adapter

TestPrism can import the adapter directly:

```text
integrations.testprism.agent:TestHelixAgent
```

The complete adapter example is in
`integrations/testprism/agent.example.json`. For a batch run, use the official
TestPrism environment and keep all inputs and outputs outside both source
checkouts:

```bash
python -B -m integrations.testprism \
  --bench /path/to/TestPrism \
  --dataset /path/to/staged-dataset \
  --selection /path/to/selection.json \
  --patch-sets /path/to/frozen-patch-sets \
  --config configs/testprism.quickstart.json \
  --task-id source/public-task-id \
  --out /path/to/results \
  --concurrency 1 --patch-concurrency 1
```

Run the same command with `--check` first for a no-model preflight.

## 5. Score Officially

TestHelix's internal `validated` and `provisional` states are diagnostic only.
Use the unmodified TestPrism runner and its frozen valid/invalid controls. A
task passes only when the official result reports `is_resolved: true`.

Validate the local integration contract after any TestPrism upgrade:

```bash
npm run check
npm run build
python -B tests/testprism_integration_contract.py
```
