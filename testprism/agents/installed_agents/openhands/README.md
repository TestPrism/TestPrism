# OpenHands Agent

OpenHands agent (with local runtime) can be used in terminal-bench.

OpenHands needs at least `LLM_API_KEY` to work. For custom LLM providers you typically
also need `LLM_BASE_URL`. For Azure OpenAI, add `LLM_API_VERSION`.

## TestBench: where `run-tests.sh` must live

The harness expects tests under **`{task.yaml workdir}/agent-tests/`** (for example
`/app/agent-tests/run-tests.sh`). OpenHands uses `SANDBOX_VOLUMES` to bind that tree
into its inner sandbox.

**You do not have to put filesystem paths inside `qwen.json` by default.** The OpenHands
integration resolves the task root automatically by, in order:

1. Environment variable **`TESTPRISM_TASK_WORKDIR`** if set (optional; not set by TestPrism today).
2. **`TASK_WORKDIR`** from your `--agent-config` JSON under `"env"` if you set it (use this when auto-detection is wrong for a dataset).
3. Otherwise: look for an existing **`agent-tests`** directory (TestBench creates it before the agent runs) under common roots (`$PWD`, `/app`, `/workspace`, …) or under `$PWD` via `find`.

So `configs/qwen.json` stays like Claude Code’s config: mostly **API keys and model**. Only add `"TASK_WORKDIR": "/app"` (or `/testbed`, etc.) when you run tasks whose `workdir` is not discoverable by the heuristics above.

## Environment variables

Example command:

```bash
export LLM_BASE_URL=<optional base url>
export LLM_API_KEY=<llm key>
tb run \
    --dataset-name terminal-bench-core \
    --dataset-version 0.1.1 \
    --agent openhands \
    --model <model> \
    --cleanup
```

## JSON config (`--agent-config`)

You can pass a JSON file (same flag as claude-code / agentscript) so secrets and
endpoints are written into the task container’s `setup-env.sh` without exporting them
on the host:

```bash
tb testbench run \
    --dataset-path tasks \
    --agent openhands \
    --agent-config testprism/agents/installed_agents/openhands/configs/qwen.json
```

Copy an existing file under [`configs/`](./configs/), replace placeholders, and adjust
`LLM_BASE_URL` / optional top-level `model` as needed. CLI `--model` still wins when both
are set.

### CloudGPT sidecar（SSH 反向隧道 / `localhost:23437`）

当 LLM 走宿主机上的 Azure 兼容 sidecar（例如 `http://localhost:23437/openai/deployments/...`，客户端使用占位 `api_key`）时，使用 [`configs/cloudgpt-sidecar.json`](./configs/cloudgpt-sidecar.json)：

- **`model`**：`azure/<deployment>`，其中 `<deployment>` 与 sidecar 文档中的 deployment 名一致（默认示例为 `gpt-4o-mini-20240718`）。
- **`AZURE_OPENAI_AD_TOKEN`**：`dummy`（推荐）。本地 sidecar 场景下 OpenHands 会走 Bearer 头，避免 `api-key` 触发 401。
- **`LLM_BASE_URL`**：任务容器内需能访问到**宿主机**上监听 `23437` 的地址。推荐直接写 **`http://172.17.0.1:23437`**（以本机 `docker0` 网关为准）。
- **`LLM_API_VERSION`**：与 sidecar 一致（示例为 `2025-04-01-preview`）。

在宿主机上仍需保证隧道与健康检查（例如 `source ~/cloudgpt-env.sh` 后执行 `cgpt-ping`）。OpenHands 现已对容器内地址做了兼容：若检测到 `LLM_BASE_URL` / `AZURE_API_BASE` 是 `localhost`/`127.0.0.1`/`::1`，会自动改写为 `172.17.0.1`。如你的 Docker 网关不是该地址，可在宿主机额外设置 `OPENHANDS_CONTAINER_HOST_GATEWAY=<your-gateway-ip>` 覆盖默认值。

### 阿里百炼（DashScope / Model Studio）

在控制台创建 **API Key**（一般在「API Key」或「密钥」入口；导航里也可打开 **API 参考** 查看调用说明）。走 **OpenAI 兼容模式** 时，北京地域常用：

- `LLM_BASE_URL`: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- `LLM_API_KEY`: 控制台里的 DashScope / 百炼 API Key
- `model`: 在百炼里选用的模型名，OpenHands 经 LiteLLM 时常用 `openai/<模型名>` 形式（例如 `openai/qwen-plus`），若报错可改为控制台文档给出的名称或去掉 `openai/` 前缀再试

可直接复制 [config/bailian-beijing.json](./config/bailian-beijing.json)，把 `LLM_API_KEY` 换成你的 Key。新加坡等国际地域请改用官方文档中的 `dashscope-intl.aliyuncs.com` 等地址，见 [OpenAI 兼容调用说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)。

Please see [OpenHandsAgent](./openhands_agent.py) for how variables are resolved. To
override other OpenHands settings from the host, export variables prefixed with
`OPENHANDS_` (the prefix is stripped when passed into the container). For example, for
[max budget per task](https://docs.all-hands.dev/usage/configuration-options#task-management)
use `export OPENHANDS_MAX_BUDGET_PER_TASK=10`.
