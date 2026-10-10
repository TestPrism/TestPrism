# TestHelix 与 TestPrism 接入

本目录提供 TestPrism 官方 `BaseAgent` 的适配器。TestPrism 本体、任务数据、Docker 镜像和评分产物属于外部环境，不复制进本仓库。

## 入口

- `agent.py`：实现官方 agent 接口，调用宿主上的 TestHelix Node agent。
- `runner.py`：执行环境预检、任务选择和官方 harness 参数转接。
- `staging.py`：将公开任务复制到独立工作区，避免修改外部 checkout。
- `cleanup.py`：只清理由本次运行 owner label 标记的 Docker 资源。
- `agent.example.json`：外部 harness 使用的导入路径和参数模板。

## 准备

在 TestHelix 根目录安装 Node 依赖并构建：

```bash
npm ci
npm run build
```

准备一个完整的 TestPrism Python 环境，并在宿主环境设置模型凭据：

```bash
export TESTHELIX_API_KEY=your-key
export TESTHELIX_BASE_URL=https://api.openai.com/v1
export TESTHELIX_MODEL=gpt-5.5
```

凭据只通过环境变量传递，不写入配置文件、任务描述、日志或容器。需要额外请求头时，在配置中使用 `headersEnv` 指向环境变量名。

## 预检与运行

先执行无副作用预检：

```bash
python -B -m integrations.testprism --bench /path/to/TestPrism --check
```

接着使用公开任务清单运行。路径、清单和 patch sets 应位于外部 benchmark 目录之外：

```bash
python -B -m integrations.testprism \
  --bench /path/to/TestPrism \
  --dataset /path/to/staged-dataset \
  --selection /path/to/selection.json \
  --patch-sets /path/to/frozen-patch-sets \
  --config configs/testprism.quickstart.json \
  --task-id source/task-id \
  --out .runs/testprism-run \
  --concurrency 1 --patch-concurrency 1
```

`TESTPRISM_QUICKSTART.md` 说明了 Node CLI 的等价流程，包括 `doctor`、`inventory`、`prepare-task` 和 `cleanup`。本地开发可使用 `runtime.kind=local`；正式 benchmark 任务必须使用官方 Docker 容器。

## 数据与评分边界

适配器只把公开的 task instruction、task ID、workdir 和时间限制传给 TestHelix。模型上下文不包含 reference patch、solution、oracle 标签、patch sets 或宿主凭据。生成阶段完成后，仅 `agent-tests/` 会复制回官方任务工作区。

预检还会从公开 `task.yaml` 推断任务合同：输入/格式约束、required outputs、任务是
`tests-only` 还是 `source-repair`，以及宿主硬约束（公开任务优先、`agent-tests/` 根目录、
`run-tests.sh`、禁止隐藏 benchmark 数据和有限 artifact）。官方 tests-only 适配器遇到
明确要求 `fixed_*.py`、corrected source 或 source modification 的任务会在模型调用前失败，
避免把 source-repair 任务误送入只提交 `agent-tests/` 的协议。模型的 `submit_candidate`
也可以提供结构化输入输出合同；声明不能覆盖宿主推断或放宽硬约束。

结构化 kind、字段级格式校验、宿主默认值和实际文件检查由
`src/task-contract.ts` 实现并由 `tests/task-contract.test.ts` 覆盖。运行时通道和自由文本明确保留为待验证项。

TestHelix 的 `validated`、`provisional` 和 adapter 自检只表示内部流程状态。最终结果必须由 TestPrism 官方 runner 使用冻结的 valid/invalid 控制集计算；只有官方结果文件中的 `is_resolved: true` 才计为通过。

## 输出与清理

每个任务的输出目录包含公开 task descriptor、Node 日志、事件、预算账本和 `submission/agent-tests/`。不要在排查时打印完整会话、环境变量或配置内容。

```bash
node dist/cli.js cleanup --out .runs/testprism-run
```

清理命令按 owner label 定位资源，不执行全局 Docker prune。`.runs/`、TestPrism checkout 和任务镜像均由 `.gitignore` 排除。

## 验收

```bash
npm run check
npm run build
python -B tests/testprism_integration_contract.py
```

若外部 TestPrism 版本升级，应重新运行预检和契约测试，并记录官方 runner、任务清单和 patch sets 的版本摘要。适配器不修改外部仓库，也不把内部运行结果当作官方分数。
