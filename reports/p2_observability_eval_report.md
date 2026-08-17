# P2 轨迹观测与可复现实验

## 1. 基线

- 起始 Commit：`8a508fbb5638fc5c0f2f218f30253a2aea9985e9`（P1 收尾提交）
- 分支：`refactor/evocoder-runtime-v2`
- Python 版本：容器内 `3.11.15`（`evocoder-sandbox:latest`）；宿主机 `3.14.6` 仅用于开发期快速回归
- 修改前测试结果：`187 passed, 0 failed`；本阶段结束为 `198 passed, 0 failed`
- 工作区原先已有未提交的 `docs/` 删除和 `wiki/docs/` 未跟踪文件。本阶段未覆盖、未删除、未纳入 Commit。
- 沙箱验证：`docker run --rm --entrypoint bash -v "$PWD:/work" -w /work evocoder-sandbox:latest -lc 'pip install -q -e ".[dev]" && python -m ruff check agents tests benchmarks && python -m pytest -q'` → `All checks passed!` / `198 passed in 4.01s`

## 2. 完成内容

| 改造项 | 对应文件 | 实现方式 | 为什么采用该设计 |
|--------|----------|----------|------------------|
| 事件级 Trace | `agents/trace.py` | JSONL 追加写入；字段全集始终出现；Prompt 默认 hash；密钥键名与 `sk-` / Bearer 脱敏 | 一次运行可以从配置追到模型、工具、Skill 版本和错误，且默认不泄漏隐私 |
| Runtime 接线 | `agents/agent.py`、`agents/main.py` | `run_start` 到 `run_finished`；模型请求/响应、工具请求、策略、Memory、折叠、Skill 检索、子 Agent；`--trace` | 子 Agent 共享父 Recorder，用 `parent_step_id` 挂到同一 `run_id` |
| Skill 治理事件 | `agents/online_skill_evolution.py`、`agents/skill_shadow_eval.py` | Candidate 落盘发 `skill_candidate_created`；成对评测结束发 `skill_evaluated` | 评测产物能定位 Candidate / Champion 版本 |
| 统一预算 | `agents/runtime_config.py`、`agents/agent.py` | `BudgetTracker.add_usage` 只累加已知 Usage；缺失标 `unknown`，绝不写成 0；Side Query 走 `_trace_side_query_call` | `max_cost` 看到主模型 + Memory/折叠/抽取/Judge 的完整统计；父子共享同一 tracker |
| Benchmark Runner | `benchmarks/` | 冻结 config/manifest/task；脚本化 OpenAI；产物 `runs/<run_id>/` | CI 可重跑，不依赖付费 API；失败样本保留 |
| CI | `.github/workflows/ci.yml` | `ruff check agents tests benchmarks` + `pytest -q` | Fake 模型/MCP/Scheduler/Policy/Plan/Session/Skill/Trace/Benchmark 都在无 Key 下跑 |
| 文档口径 | `README.md`、`wiki/评测部分.md`、`wiki/简历包装.md`、`wiki/技术亮点.md` | GAIA/HLE 数字标「历史未验证」；可引用指标映射到 `benchmarks/` 产物 | 仓库代码 + Trace 复现不了的分数不再当当前结果宣传 |

## 3. Trace 事件

每条事件包含：`timestamp, run_id, session_id, step_id, parent_step_id, event_type, model, provider, prompt_hash, tool_call_id, tool_name, skill_name, skill_version, permission_decision, latency_ms, input_tokens, output_tokens, estimated_cost, exit_code, artifact_path, error_type`。

覆盖：`run_start, model_request, model_response, tool_requested, policy_decision, tool_started, tool_finished, memory_retrieved, context_compacted, skill_retrieved, skill_candidate_created, skill_evaluated, subagent_started, subagent_finished, run_error, run_finished`。

禁止写入 API Key、Authorization、未脱敏环境变量。Prompt 正文需 `--trace` 且 `BEAR_TRACE_BODIES=1`，写入前仍脱敏。`input_tokens` 等 schema 字段不会被当成密钥抹掉。

## 4. 成本与预算

- 主循环模型调用、Side Query（Memory 预取、上下文折叠、Skill Extractor/Maintainer/Judge）都走同一套 `parse_model_usage` + `BudgetTracker.add_usage`。
- 子 Agent / Skill fork 继承同一 `budget`，父 Run 总成本是全部子调用之和。
- Provider 没返回 Usage 时：`unknown_usage_count += 1`，token 保持原值，Trace 里 `usage_status=unknown`，`input_tokens`/`output_tokens` 为 `null`。
- OpenAI 流式组装不再把缺失 Usage 填成 `0`。
- `cost_usd()` / `snapshot()["estimated_cost_usd"]` 在存在 unknown usage 时为 `null`，绝不回退成 `0.0`。若同时设置了 `max_cost`，按不完整记账保守停止，避免 `--max-cost` 被空 Usage 绕过。
- 每次根 `chat()` 分配新的 `run_id` 并截断/新建对应 JSONL；子 Agent / Skill fork 的模型事件 `parent_step_id` 指向对应的 `subagent_started`。Skill 后台任务在 `run_finished` 之前 `await drain`。
- `validate_trace()` 校验唯一根、悬空父节点、以及 `run_finished` 之后是否还有事件。
- Benchmark 遇到已有 `run_id` 会完整重建目录；`tools` / `memory_enabled` / `folding_enabled` / `temperature` / `seed` 接入 Runtime；manifest 校验 `task_ids` 与 `task_digest`。
- 脱敏同时覆盖 `x-api-key`、`proxy-authorization` 等 `-` / `_` / 大小写变体，以及嵌套 `tool_arguments`。

## 5. Benchmark 与产物

```text
python -m benchmarks.runner
```

冻结项：数据集版本、任务 ID、模型、provider、temperature、预算、工具集、Skill 版本、Memory/折叠开关、超时、种子、Git SHA。

```text
runs/<run_id>/
├── config.json
├── manifest.json
├── results.jsonl
├── traces/<task_id>.jsonl
├── artifacts/
└── summary.json
```

当前任务集是 `software-engineering-mini-v1`（写文件、先读后改、读文件问答、故意失败的 `se-wrong-output`）。失败样本留在 `results.jsonl`，不剔除。CI 使用脚本化模型，不需要真实 Key。

`summary.json` 指标：Task Success Rate、平均 Tool Steps、P95 Latency、平均 Token/成本、Hard Failure、NTR、三臂成对差值。NTR / 成对差值来自同一次 run 里的脚本化 Skill Shadow 评测。

## 6. 文档

- `wiki/评测部分.md`、`wiki/简历包装.md`、`wiki/技术亮点.md` 中的 GAIA 53.3 / HLE 20.2 / 折叠消融 44.7 全部标为历史未验证。
- 当前 Runtime 没有多模态视觉工具，不再把 MM 分数当能力宣传。
- README 增加 Trace 与 `python -m benchmarks.runner` 入口；可引用指标只来自 `runs/<run_id>/summary.json`。

## 7. 测试

| 测试 | 覆盖 |
|------|------|
| `tests/unit/test_trace.py` | schema、hash、连字符 Header 脱敏、图结构、unknown 成本为 null 且 `max_cost` 保守停 |
| `tests/integration/test_trace_agent_loop.py` | 模型-工具-策略链路、两次 chat 不同 `run_id`、子 Agent 挂到 `subagent_started`、Candidate 在 `run_finished` 之前、缺失 Usage |
| `tests/integration/test_side_query_budget.py` | Side Query 计入父预算；父子共享 tracker |
| `tests/integration/test_benchmark_runner.py` | 冻结产物、每题轨迹、失败样本保留、Skill 版本、同一 `run_id` 重跑除时间/ID 外一致 |

CI 仍覆盖既有 Fake OpenAI/Anthropic/MCP、Scheduler、Policy、Plan Mode、Session/Memory、Skill Promote/Rollback。

Ruff 对历史 `agents/agent.py` 等文件保持 per-file ignore（P0 已记录的存量告警）；CI 扫描 `agents tests benchmarks`，避免把 `.bear/skills` 用户语料算进 lint。

## 8. 完成标准对照

| 标准 | 结果 |
|------|------|
| 每次运行有唯一 `run_id` | `TraceRecorder` 与 Benchmark `run_id` |
| 模型/工具/Memory/Skill/子 Agent 可串成轨迹 | 上述事件 + `parent_step_id` |
| Side Query 计入成本和预算 | `_trace_side_query_call` |
| Skill 评测能定位 Candidate/Champion | `skill_evaluated` + evaluation artifact |
| Benchmark 固定命令可重跑且每题有结果和轨迹 | `python -m benchmarks.runner` |
| CI 不需真实 Key | 脚本化 OpenAI + 现有 Fake MCP |
| 文档指标映射到产物 | wiki/README 指向 `benchmarks/` 与 `runs/<run_id>/` |

P2 到此结束，未进入后续阶段。

## 9. 审查返工（`2ea0fca` 阻断项）

外部审查对 `2ea0fca` 给出 4 个阻断项后，本提交只修这些项，未进入后续阶段。

| 阻断项 | 修复 |
|--------|------|
| 同一 `run_id` 多次 `run_start`；子 Agent 未挂到 `subagent_started`；Candidate 丢在 `run_finished` 之后 | 每次根 `chat()` 新 `run_id`；子 Agent `_run_step_id = subagent_started.step_id`；Skill 任务在 `run_finished` 前 drain；图结构校验 |
| 相同 `--run-id` 重跑污染 Trace / Skill 版本 / tool steps | 已有目录整棵删除后重建；冻结配置真正传入 Runtime；manifest 校验任务 ID 与内容摘要 |
| unknown usage 仍以 `0.0` 参与 `max_cost` | 聚合成本为 `null`；设置了 `max_cost` 时按不完整记账停止 |
| `x-api-key` / `proxy-authorization` 未脱敏 | 键名先 `lower().replace("-", "_")` 再匹配，并覆盖嵌套工具参数 |

复验建议：清理沙箱代理环境变量后跑 `ruff check agents tests benchmarks` 与 `pytest -q`，并确认同一 `--run-id` 连续两次除时间/ID 外结果一致。本轮沙箱结果：`208 passed`，Ruff 全绿。
