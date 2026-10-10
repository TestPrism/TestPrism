# AgentScript image base layer

Inject Node 20 and `~/.agentscript/node_modules` into any TestPrism **client image** without editing per-task `Dockerfile`s.

Harness expects client images named: `tb__<task-id>__client:latest` (`task_id` is the task directory name).

## Files

| File | Purpose |
|------|---------|
| `build.sh` | Build `testprism-agentscript-base:latest` (run once) |
| `layer-on-image.sh` | Overlay the base onto an existing image → `tb__*__client` |
| `Dockerfile` / `package.json` | Required by `build.sh`; do not remove |

## Usage

### 1. Build the base (once)

```bash
cd testprism/agents/installed_agents/tex_t/agentscript-base
chmod +x build.sh layer-on-image.sh
./build.sh
```

Custom tag: `./build.sh my-registry/agentscript-base:v1`

### 2. Layer onto a task image

```bash
./layer-on-image.sh <source-image> tb__<task-id>__client:latest
```

Example (QuixBugs Java Hanoi):

```bash
./layer-on-image.sh \
  testprism-prepull-from-quixbugs-quixbugs-java-hanoi:latest \
  tb__quixbugs-java-hanoi__client:latest
```

List local source images:

```bash
docker images | grep -E 'prepull|tb__'
```

### 3. Run the benchmark

```bash
cd TestPrism   # repository root
.venv/bin/tb run -p <dataset-path> \
  -a agentscript \
  --agent-config testprism/agents/installed_agents/tex_t/configs/local_sidecar.json \
  -t <task-id> \
  --no-rebuild \
  --no-cleanup
```

### Environment variable

```bash
export TESTPRISM_AGENTSCRIPT_BASE=testprism-agentscript-base:latest
```

### Verify

```bash
docker run --rm tb__quixbugs-java-hanoi__client:latest node -v
docker run --rm tb__quixbugs-java-hanoi__client:latest \
  test -d /root/.agentscript/node_modules && echo OK
```

## Other benchmarks

Same flow: use your local prepull or already-built task image as the source; target tag is always `tb__<task-id>__client:latest`.

For batch prepull, see `TestPrism/docker/agentscript-base/layer-quixbugs-*.sh`, or loop `layer-on-image.sh` over filtered `docker images`.

## Notes

- This layer includes **Node + npm dependencies only**; Java Gradle, Python `uv`, etc. must still be provided by the task image when needed.
- `install-agent.sh` still writes the latest `runner.mjs`; `npm install` should report `up to date` when deps are preinstalled.
- With `-a agentscript` and **without** `--no-rebuild`, the harness can append `COPY --from` this base during image build (requires `./build.sh` first).

For LLM config and agent constraints, see [AGENTSCRIPT.md](../AGENTSCRIPT.md).
