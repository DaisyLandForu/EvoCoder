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
- 第一轮实现 Commit：`4b4d7b4b8e7ac7d835216a88883dc970863b24a4`
- 未纳入本 Commit 的已有工作区改动：`docs/feishuDownload/*`、`docs/localHistory/*` 删除，以及未跟踪的 `wiki/docs/`

## 8. 第二轮返工：外部审查缺陷修复

外部审查在 `4b4d7b4` 上判定 P0 暂不通过，列出 5 个阻断缺陷和 2 个次要缺口。本节记录逐项修复。

### 8.1 修复项

| # | 审查发现的问题 | 根因 | 修复方式 | 对应文件 |
|---|----------------|------|----------|----------|
| 1 | 调度器改变真实执行顺序：`write_file → read_file` 实际按 `read_file → write_file` 执行，读到旧内容 | 调度分成“全部只读并发 + 全部写串行”两段，读被整体提到写前面 | 改为按原始顺序切段：连续只读工具组成一个并发批，任何副作用工具单独成段并作为屏障 | `agents/tool_scheduler.py` |
| 2 | Plan Mode 实际写不了计划文件：授权 allow，执行阶段报 `path escapes workspace` | 计划文件在 `~/.bear/plans/`，授权侧特判跳过、执行侧仍走 Workspace 限制，两边边界不一致 | `PolicyEngine.plan_file_path` 改为属性并维护解析后的允许路径，`resolve_workspace_path` 统一放行该单个文件；删掉授权侧的特判 | `agents/policy.py` |
| 3 | 权限模式与 CLI 承诺不符：`default` 不逐项审批编辑、`acceptEdits` 不再确认危险 Shell、`dontAsk` 反而执行 Shell | P0 重写策略时丢失了原 `check_permission` 的确认矩阵，且 `run_shell` 分支在 `dontAsk` 判断之前返回 | 用 `_confirmation_message()` 统一判定“是否需要确认”，再按模式收敛为 allow/confirm/deny；同时把 `settings.json` 的 allow/deny 规则和危险命令识别并入 `PolicyEngine` | `agents/policy.py`、`agents/tools.py` |
| 4 | OpenAI 触发预算上限后产生非法消息链，停在 `assistant(tool_calls)` | 只有 Anthropic 分支补了 skipped tool result | 为每个未执行的 `tool_call_id` 回写预算终止结果后再 break，与 Anthropic 对齐 | `agents/agent.py` |
| 5 | 仓库级 MCP 信任可绕过：未信任的 `.bear/settings.json` 里的 Server 仍被启动 | 信任哈希只覆盖 `.mcp.json`，加载器却还读项目内 `.bear/settings.json` | 信任哈希覆盖全部仓库级声明文件；未信任时只加载用户级 `~/.bear/settings.json`，仓库级配置整体跳过 | `agents/policy.py`、`agents/mcp_client.py` |
| 6 | `model_timeout_s` 只有字段没有作用 | 模型调用未套超时 | `_call_model_with_timeout()` 对每次调用尝试套 `asyncio.wait_for`，`<=0` 表示关闭 | `agents/agent.py` |
| 7 | `--resume` 只恢复两组消息 | main 只传了 `anthropicMessages`/`openaiMessages` | 同时恢复 `foldedSessionMemories`、原 Session ID 与 startTime，并保证 OpenAI System Prompt 位于首条 | `agents/main.py`、`agents/agent.py` |

修复后的权限矩阵（`tests/unit/test_permission_matrix.py` 逐格断言）：

| 模式 | 读文件 | 写/编辑文件 | 安全 Shell | `rm a.txt` | skill 写入 |
|------|--------|-------------|------------|------------|------------|
| `plan` | allow | deny（计划文件除外） | deny | deny | deny |
| `default` | allow | confirm | allow | confirm | confirm |
| `acceptEdits` | allow | allow | allow | confirm | allow |
| `dontAsk` | allow | deny | allow | deny | deny |
| `bypassPermissions` | allow | allow | allow | allow | allow |

### 8.2 测试证据（第二轮）

命令与第 4 节相同，镜像 `evocoder-sandbox:latest`：

```bash
docker run --rm --network=bridge --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp/evocoder-home -e PYTHONPATH=/workspace \
  -e PYTHONDONTWRITEBYTECODE=1 -e RUFF_CACHE_DIR=/tmp/ruff-cache \
  -v "$PWD:/workspace" -w /workspace --entrypoint bash \
  evocoder-sandbox:latest \
  -lc 'pip install --user -q pytest pytest-asyncio ruff && python -m pytest -q -o cache_dir=/tmp/pytest'
```

- 通过数量：87（第一轮 36）
- 失败数量：0
- Ruff：`agents/policy.py`、`agents/tool_scheduler.py`、`agents/mcp_client.py`、`agents/runtime_config.py`、`agents/plan_mode.py`、`tests/` 全部通过；全库历史告警由 144 条降到 141 条（顺手删除两个未使用 import，未做全库重写）
- CLI Smoke：`python -m agents.main --help` 成功

新增/加强的测试：

| 缺陷 | 测试 |
|------|------|
| 1 | `test_read_after_write_is_not_hoisted_before_the_write`、`test_read_segments_around_a_write_run_in_order`、`test_consecutive_read_only_calls_still_run_concurrently` |
| 2 | `tests/integration/test_plan_file_write.py` 全部 3 例、`test_agent_can_write_its_plan_file_through_the_tool_chain`（经由 `Agent._execute_tool_call` 真实工具链，不再用 `Path.write_text` 造数据） |
| 3 | `tests/unit/test_permission_matrix.py`（5 模式 × 6 场景参数化 + 4 条专项断言） |
| 4 | `test_openai_budget_stop_closes_every_tool_call`、`test_anthropic_and_openai_agree_on_budget_closure` |
| 5 | `tests/unit/test_mcp_trust.py` 全部 4 例 |
| 6 | `test_model_timeout_is_enforced`、`test_model_timeout_disabled_when_not_positive` |
| 7 | `test_resume_restores_folded_memory_and_session_identity` |
| 全局 | `tests/conftest.py` 增加 autouse 的 `isolated_home`，测试不再读写真实 `~/.bear` 与 `~/.bear-code` |

### 8.3 仍未处理

- 第 6 节列出的风险项保持不变（容器化 Shell 后端、MCP Streamable HTTP、历史 Ruff 告警、未跑真实模型）。
- Plan 模式下对同一计划文件的第二次写入仍受“先读后写”保护约束，需要模型先 `read_file`。这是既有行为，本轮未改。
- P1 未开始。
