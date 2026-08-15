# P0 Runtime 正确性与长程状态基础

## 1. 基线

- 开始 Commit：`cacc4e89d3a52eece5a947439fb8371652f274ba`
- 分支：`refactor/evocoder-runtime-v2`（新建）
- Python 版本：容器内 `3.11.15`（`evocoder-sandbox:latest`）；宿主机 `3.14.6` 未用于测试
- 修改前测试结果：仓库无测试结构，`pytest -q` 为 `no tests ran`
- 已存在但未处理的问题：
  - 工作区原先已有未提交的 `docs/` 删除和 `wiki/docs/` 未跟踪文件。本阶段未覆盖、未删除、未纳入 Commit。
  - 历史 GAIA/HLE 评测 runner 不在本仓库中，P0 未补。
  - 旧文件仍有历史 Ruff 告警（`agents/agent.py`、`agents/tools.py` 等合计约 63 条，多为 `E741`/`E501`）。未为清告警重写全库。

## 2. 完成内容

| 改造项 | 对应文件 | 实现方式 | 为什么采用该设计 |
|--------|----------|----------|------------------|
| 测试基线 | `pyproject.toml`、`tests/` | 增加 pytest / pytest-asyncio / Ruff 与 `unit`/`integration`/`fakes`/`fixtures` | 让 Runtime 行为可在无真实 API 下回归 |
| Tool Call 调度器 | `agents/tool_scheduler.py`、`agents/agent.py` | OpenAI/Anthropic 规范化为 `NormalizedToolCall`，按 normalize→deduplicate→authorize→execute→commit 执行 | 消除 OpenAI 循环内重复批处理，保证 Exactly-once 和稳定回写顺序 |
| 统一 Runtime 配置 | `agents/runtime_config.py` | `RuntimeConfig` + 共享 `BudgetTracker`；子 Agent / fork Skill 用 `derive_child()` | 模型、Key、Base URL、Workspace、预算、超时同源，权限只能降不能升 |
| Plan Mode 状态机 | `agents/plan_mode.py`、`agents/agent.py` | `DEFAULT→PLANNING→AWAITING_APPROVAL→EXECUTING→DEFAULT`；审批函数 `await`；提交计划文件正文 | 修复未 await、误传路径、审批后权限被清空的问题 |
| 动作策略边界 | `agents/policy.py`、`agents/tools.py` | `PolicyEngine` 规范化路径、拦截逃逸/符号链接、Plan 下 MCP 当副作用、`.mcp.json` 显式信任、受限 Shell 后端 | 策略集中在执行前，而不是只靠 Shell 字符串黑名单 |
| MCP 可靠性 | `agents/mcp_client.py` | 先登记 Future 再发送；initialize/list/call 独立超时；消费 stderr；`terminate/kill` 后 `wait`；保留 `isError` | 避免响应竞态、超时泄漏、stderr 撑满死锁 |
| Session / Memory / 折叠 | `agents/session.py`、`agents/memory.py`、`agents/session_memory.py` | Session 按规范化 Workspace hash 隔离；中文查询可预取 Memory；三层折叠 Schema 校验与降级 | 长程任务按项目恢复，损坏折叠状态不会直接注入模型 |

## 3. 关键调用链

### 改造前

```text
模型响应
  → OpenAI 在 for tool_calls 循环内反复组 batch / Anthropic 流式提前执行
  → check_permission()
  → 直接 execute_tool / MCP / 子 Agent(bypassPermissions)
  → 按完成顺序回写
  → 全局 ~/.bear-code/sessions
```

### 改造后

```text
模型响应
  → ToolCallScheduler.normalize_{openai,anthropic}
  → deduplicate(tool_call_id)
  → PolicyEngine.authorize
  → 只读并发 / 写与 Shell 串行
  → 按 source_index 回写 tool result
  → 共享 BudgetTracker
  → 按 workspace_id 保存 Session / 校验折叠状态
```

## 4. 测试证据

- 测试命令（`evocoder-sandbox:latest`）：

```bash
docker run --rm --name evocoder-p0-test --network=bridge \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  -e HOME=/tmp/evocoder-home -e PYTHONPATH=/workspace \
  -e PYTHONDONTWRITEBYTECODE=1 -e RUFF_CACHE_DIR=/tmp/ruff-cache \
  -v "$PWD:/workspace" -w /workspace --entrypoint bash \
  evocoder-sandbox:latest \
  -lc 'pip install --user -q pytest pytest-asyncio ruff && python -m pytest -q --tb=short -o cache_dir=/tmp/pytest'
```

- 通过数量：36
- 失败数量：0
- CLI Smoke：`python -m agents.main --help` 成功；核心模块 import 成功
- 关键测试覆盖的行为：
  - 两个不同 Tool Call 各执行一次
  - 重复 `tool_call_id` 只执行一次
  - 三个并发只读工具按原始顺序回写
  - 一个工具失败不影响其他独立结果
  - OpenAI 与 Anthropic 共用 Scheduler
  - 写工具不无约束并发
  - 子 Agent 继承 OpenAI/Anthropic 配置且不自动 bypass
  - Plan 子 Agent 不能写文件和 Shell
  - 子 Agent token 计入父预算
  - Plan Mode 审批 `await`、拒绝保持只读、计划正文提交审批、双协议 System Prompt 同步
  - `/etc/passwd`、`../`、外指符号链接被拒绝
  - Plan Mode 未知 MCP 删除类工具被拒绝
  - 未信任 `.mcp.json` 不会启动 Server
  - Fake MCP：initialize/list、快速响应、超时清理、崩溃、stderr 洪泛、单 Server 失败隔离
  - Session 按项目隔离；中文 Memory 预取；三层折叠序列化/损坏降级

## 5. 验收结果

| 验收标准 | 是否通过 | 对应测试或产物 |
|----------|----------|----------------|
| OpenAI/Anthropic 使用统一 ToolCallScheduler | 通过 | `test_openai_and_anthropic_share_scheduler`、`test_scheduler_used_by_agent_dispatch` |
| 多工具调用不存在重复执行 | 通过 | `test_duplicate_tool_call_id_executes_once` |
| Tool Result 顺序稳定 | 通过 | `test_concurrent_readonly_results_keep_source_order` |
| Plan Mode 状态转换正确 | 通过 | `tests/unit/test_plan_mode.py` |
| 主/子/fork 使用统一 RuntimeConfig | 通过 | `tests/unit/test_runtime_config.py`、`test_subagent_inherits_custom_openai_base_url` |
| 子 Agent 不能权限升级 | 通过 | `test_general_child_does_not_auto_bypass`、`test_child_cannot_bypass_parent_policy` |
| 路径逃逸和未授权 MCP 启动被阻止 | 通过 | `tests/unit/test_policy.py`、`test_untrusted_mcp_json_does_not_start_servers` |
| MCP 请求具有超时和异常清理 | 通过 | `tests/unit/test_mcp_client.py` |
| Session 按项目隔离 | 通过 | `test_sessions_are_isolated_by_workspace` |
| 中文 Memory 召回触发正常 | 通过 | `test_chinese_query_triggers_memory_prefetch` |
| 三层折叠状态可恢复 | 通过 | `test_folded_memory_roundtrip`、`test_corrupt_fold_json_falls_back` |
| 新增关键路径有自动化测试 | 通过 | `tests/unit/*`、`tests/integration/*` |
| 核心 CLI Smoke | 通过 | `python -m agents.main --help` |
| 新增文件无 Ruff 错误 | 通过 | 新模块与 `tests/` ruff check 通过 |
| 本阶段修改文件不新增以清历史为目的的全库重写 | 通过 | 旧文件历史告警保留并记录 |

## 6. 未完成项与风险

- 完整 Docker 网络隔离的 Shell 后端未作为默认执行器，只留了 `ShellBackend` 接口。
- MCP Streamable HTTP 未实现，仍是 stdio JSON-RPC。
- 旧 `agents/agent.py` / `agents/tools.py` 仍有历史风格和 Ruff 告警。
- 真实模型 one-shot 未跑，避免调用付费 API 和写入宿主机。
- P1 的 Candidate/Gate/Promote 未开始。

下一阶段依赖：P0 Runtime 调度、策略、Session 隔离和折叠 Schema 已成为 P1 Skill 治理的执行底座。需用户明确发送“开始 P1”后才能实施。

## 7. Git 信息

- 阶段开始 Commit：`cacc4e89d3a52eece5a947439fb8371652f274ba`
- 阶段结束 Commit：见提交后输出的 SHA
- 未纳入本 Commit 的已有工作区改动：`docs/feishuDownload/*`、`docs/localHistory/*` 删除，以及未跟踪的 `wiki/docs/`
