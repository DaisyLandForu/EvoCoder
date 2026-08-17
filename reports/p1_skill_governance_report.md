# P1 Skill 自进化治理

## 1. 基线

- 起始 Commit：`77357d1`（P0 收尾提交）
- 分支：`refactor/evocoder-runtime-v2`
- Python 版本：容器内 `3.11.15`（`evocoder-sandbox:latest`）；宿主机 `3.14.6` 仅用于开发期快速回归
- 修改前测试结果：`103 passed, 0 failed`；本阶段结束为 `171 passed, 0 failed`
- 修改前的链路：`用户反馈 → 直接改写 Active Skill → 事后人工评估`
- 修改后的链路：`用户反馈 → Candidate → Shadow 评测 → Gate → Champion → Active → Rollback`

## 2. 完成内容

| 改造项 | 对应文件 | 实现方式 | 为什么采用该设计 |
|--------|----------|----------|------------------|
| Candidate 与 Active 物理隔离 | `agents/skill_registry.py`、`agents/online_skill_evolution.py` | 在线抽取的 add/merge 一律落到 `.bear/skill-evolution/candidates/<skill>/<version>/`，`create_skill`/`evolve_skill` 不再出现在在线链路上 | 让"反馈改不到线上"成为目录结构层面的事实，而不是靠调用方自觉 |
| 一次性事实脱敏 | `agents/skill_registry.py` | `sanitize_skill_payload()` 在写盘前替换 URL、邮箱、账号、密钥、绝对路径、精确日期、长数字，并把命中类型记进 registry | 抽取提示词只是"建议模型别写"，落盘前的正则是最后一道确定性防线 |
| 生命周期状态机 | `agents/skill_registry.py` | six 状态 + `ALLOWED_TRANSITIONS` 白名单；`candidate→active` / `shadow→active` 属于 `FORCED_TRANSITIONS`，必须显式 `--force` | 非法跃迁直接被拒绝并返回原因，跳过 Gate 的行为无法悄悄发生 |
| 原子激活与快照 | `agents/skill_registry.py` | `atomic_write_text()` 走同目录临时文件 + `fsync` + `os.replace`；激活前先快照旧 Active | POSIX 下 `os.replace` 是原子的，中断只会留下临时文件，读者永远看不到半写状态 |
| 三组重新生成的成对评测 | `agents/skill_shadow_eval.py` | 每个样本在同一次运行内重新生成 `no_skill` / `current_champion` / `candidate`，三臂共用同一份冻结的 `GenerationConfig` | 结构上排除"拿 Champion 历史回复对比 Candidate 新回复"，唯一变量是注入的 Skill 段 |
| 统一评分规则 | `agents/skill_rules.py` | 规则从 Champion（无 Champion 时用 Candidate）编译一次，三臂共用；`rules_source` 写进产物 | 三臂用同一把尺子，比较的是行为差异而不是评分口径差异 |
| Replay / Holdout 隔离 | `agents/skill_shadow_eval.py` | `build_sample_pool()` 把与候选生成有交集的样本强制降级为 replay，并记录 `contaminated_holdout_ids` | Holdout 被污染是最容易发生也最难发现的失效，改成运行时强制而不是约定 |
| Judge 角色分离 | `agents/skill_shadow_eval.py`、`agents/skill_gate.py` | `JudgeConfig` 独立于 `GenerationConfig`；同模型即判定为 `non_independent`，Gate 只能给 `incubating` | 非独立 Judge 的结论可以参考，但不允许自动改变线上状态 |
| Gate 与 NTR | `agents/skill_gate.py` | `compute_paired_metrics()` + `apply_gate()`，阈值全部来自 `GateConfig`，随产物落盘 | 决策与阈值绑定存档，历史结论可以按当时的阈值复读 |
| 自动归档需显式授权 | `agents/skill_evolution.py` | `_maybe_prune_stale_skill()` 增加 `BEAR_SKILL_AUTO_ARCHIVE` 开关，默认关闭 | 移动用户 Skill 目录是不可逆操作，不应由使用统计默默触发 |
| CLI 接入 | `agents/main.py` | `/skill-status`、`/skill-eval`、`/skill-promote`、`/skill-rollback`；帮助文本提取为 `HELP_TEXT` 常量 | 生命周期每一步都有对应人工入口，且入口可被测试直接调用 |

## 3. 目录与状态机

### 存储布局

```text
.bear/skill-evolution/
├── candidates/<skill>/<version>/{SKILL.md,meta.json}   # 用户反馈的唯一落点
├── champions/<skill>/{SKILL.md,meta.json}              # 通过 Gate 的版本
├── snapshots/<skill>/<time>-<version>/{SKILL.md,meta.json}
├── evaluations/<skill>/<run_id>.json
├── holdout/{shared,<skill>}.jsonl
└── registry.json
```

Active Skill 仍在 `.bear/skills/<skill>/SKILL.md`，只能由 `promote` / `rollback` 写入。

### 状态迁移

```text
candidate ──► shadow ──► champion ──► active ──► retired
    │           │            │                      │
    │           ▼            ▼                      ▼
    └────────► rejected ◄────┴──────────────────► active（rollback）

candidate ──► active   仅限 --force，历史里记为 forced
shadow    ──► active   仅限 --force，历史里记为 forced
```

`registry.json` 中每个版本都带 `skill_name`、`version`、`parent_version`、`created_at`、`source_provenance`、`content_hash`、`status`，以及一条 `history` 列表，每次迁移记录 `time / from / to / reason / actor`。

## 4. 评测与 Gate

### 三臂生成

| Arm | System Prompt | 说明 |
|-----|---------------|------|
| `no_skill` | 基础提示词 | 不注入任何 Skill，作为绝对基线 |
| `current_champion` | 基础提示词 + Champion Skill 段 | 无 Champion 时退化为 `no_skill` 结果，并在指标里标注 |
| `candidate` | 基础提示词 + Candidate Skill 段 | 待评版本 |

三臂共用同一个 `GenerationConfig` 实例，固定 model、provider、temperature、max_output_tokens、timeout、seed、可用工具与基础系统提示词，整份配置写入产物。测试 `test_every_arm_shares_one_frozen_generation_config` 直接断言三臂拿到的是同一份配置。

### Gate 默认阈值

| 判据 | 默认阈值 | 环境变量 | 不满足时 |
|------|----------|----------|----------|
| 安全 / 格式硬失败 | 0 | `BEAR_GATE_ALLOW_HARD_FAILURES` | `reject` |
| 有效成对样本数 | ≥ 20 | `BEAR_GATE_MIN_PAIRS` | `incubating` |
| 独立 Holdout | 必须有 | `BEAR_GATE_REQUIRE_HOLDOUT` | `incubating` |
| Judge 独立性 | 必须独立 | `BEAR_GATE_ALLOW_NON_INDEPENDENT_JUDGE` | `incubating` |
| 成对任务收益 | > 0 | `BEAR_GATE_MIN_GAIN` | `reject` |
| 负迁移率 NTR | ≤ 5% | `BEAR_GATE_MAX_NTR` | `reject` |
| Token 成本增幅 | ≤ 15% | `BEAR_GATE_MAX_COST_GROWTH` | `reject` |

NTR = `#(Champion 通过而 Candidate 失败) / #(Champion 通过)`；Champion 一次都没通过时记为 `0.0`。

判定优先级：硬失败 → 无有效成对样本 → 指标阻断 → incubating 条件 → promote。硬失败排在最前，样本再少也不会被"证据不足"掩盖；反过来，样本数为 0 时不会因为收益等于 0 就误判为 reject。

`promote` 决策只把版本变成 Champion，激活仍需人工执行 `/skill-promote`。

## 5. 测试证据

沙箱内 `evocoder-sandbox:latest` 执行结果：

```text
171 passed in 3.36s
```

| 测试文件 | 用例数 | 覆盖的验收标准 |
|----------|--------|----------------|
| `tests/unit/test_skill_registry.py` | 18 | 候选隔离、provenance 字段、脱敏、非法迁移、迁移历史、promote/rollback、写入中断、registry 一致性 |
| `tests/unit/test_skill_gate.py` | 17 | NTR 公式、成本增幅、默认阈值、各阻断条件、incubating 条件、阈值配置化与环境变量 |
| `tests/unit/test_skill_cli.py` | 5 | 四个命令的接线、非独立 Judge 提示、无样本时的失败信息 |
| `tests/integration/test_skill_shadow_eval.py` | 20 | 三臂重新生成、配置冻结、仅 Skill 段不同、同一规则集、成对差异、Replay/Holdout 隔离、Judge 盲评、Gate 三种结局、完整生命周期 |
| `tests/integration/test_online_candidate_isolation.py` | 8 | 用户反馈只产出 Candidate、无法覆盖 Active、样本 provenance、脱敏、discard 不落盘、权限拒绝、自动归档需授权 |

其中几条关键断言：

- `test_interrupted_activation_leaves_the_active_skill_intact`：在 `os.replace` 上注入异常模拟激活中断，断言 Active 文件字节不变、无残留 `.tmp`、registry 的 `active_version` 未前移。
- `test_the_judge_never_sees_which_arm_it_is_scoring`：抓取所有 Judge 输入，断言不含 `candidate` / `current_champion` / `no_skill` 等身份字样。
- `test_generation_samples_cannot_serve_as_holdout`：把参与候选生成的样本放进 Holdout，断言它被强制降级为 replay 并出现在 `contaminated_holdout_ids`。
- `test_a_shared_generator_and_judge_are_marked_non_independent`：同模型 Judge 下即便指标全部达标，结论也只能是 `incubating`，Champion 目录不被写入。

Ruff：新增的 `agents/skill_registry.py`、`agents/skill_gate.py`、`agents/skill_rules.py`、`agents/skill_shadow_eval.py` 与全部新增测试为 0 告警；仓库总量从 123 条降到 118 条，剩余均为 P0 报告已登记的历史告警（`prompt.py` 38、`tools.py` 25、`agent.py` 21 等）。

## 6. 删除的旧实现

`agents/online_skill_eval.py`（1822 行）整体删除。它做的是"拿 Active Skill 的历史回复当 Champion 基线，再生成 Candidate 变体回复"，这正是 P1 明确禁止的比较方式；同时它维护了一套与 registry 平行的快照与晋升状态，两套状态并存必然漂移。其中仍有价值的规则编译与规则判定逻辑抽到了 `agents/skill_rules.py`，由三臂共用。

`/skill-eval` 从"评估在线演化质量"改为"对指定 Skill 跑成对影子评测"，参数从无参改为 `<skill> [version]`。

## 7. 未完成项与风险

- **REPL 内的 `/skill-eval` 永远只能得到 `incubating`。** 生成和判定共用会话模型，按 P1.4 属于非独立 Judge。要真正走到 `champion`，需要配置一个与生成模型不同的 Judge，或显式打开 `BEAR_GATE_ALLOW_NON_INDEPENDENT_JUDGE`。这是刻意保留的摩擦，但当前没有配置独立 Judge 的 CLI 入口。
- **Holdout 需要人工准备。** `.bear/skill-evolution/holdout/*.jsonl` 目前没有自动收集器，没有该文件时评测只能 `incubating`。自动收集需要先定义"哪些会话没有参与过技能生成"，涉及会话侧改造，放在 P1 之外。
- **`GenerationConfig` 对 side query 是记录而非强制。** 通过 `_build_side_query` 走的路径上，temperature / seed 由会话模型决定，配置字段标记为 `enforced_by="side_query"` 只作存档。要做到真正可复现，需要评测走独立的模型客户端。
- **Token 成本用估算值。** 生成器未回报 usage 时按 `len/4` 估算，成本增幅的绝对值不精确；比较的是同一估算口径下的相对差异。
- **`/skill-create` 与 `/skill-evolve` 仍直接写 Active。** 这两个是用户显式发起的手工命令，不属于"用户反馈自动改写"，本阶段保留原行为。
- **旧的 `history/` 演化记录与新的 `snapshots/` 并存。** `evolve_skill_file` 的历史目录没有迁移到 registry，只有走新链路的版本才有完整版本谱系。

## 8. Git 信息

- 分支：`refactor/evocoder-runtime-v2`
- 起始 Commit：`77357d1`
- 本阶段 Commit：`feat(p1): add gated skill evolution lifecycle`（`0888903`）；审查返工见第 9 节
- 新增文件：`agents/skill_registry.py`、`agents/skill_gate.py`、`agents/skill_rules.py`、`agents/skill_shadow_eval.py`、5 个测试文件、本报告
- 删除文件：`agents/online_skill_eval.py`
- 修改文件：`agents/main.py`、`agents/online_skill_evolution.py`、`agents/skill_evolution.py`、`tests/conftest.py`、`README.md`

## 9. 审查返工：Gate 可靠性与版本—内容一致性

针对 `0888903` 的 4 个阻断项与脱敏缺口。返工后宿主机 `180 passed in 3.05s`；P1 新增范围 Ruff 通过。

| 阻断项 | 根因 | 修复 | 新增 / 改写测试 |
|--------|------|------|-----------------|
| Champion 生成失败被计为 Candidate 胜利 | 生成异常写进 `hard_failed`/`passed`，仍作为有效 pair 参与 Gate | 区分 `infrastructure_error` 与行为失败；任一臂超时/异常/Judge 故障则该 pair 无效；`invalid_pairs > 0` 时 Gate 只能 `incubating` | `test_champion_generation_failure_does_not_count_as_a_candidate_win`、`test_a_generation_failure_invalidates_the_pair`、`test_infrastructure_failures_incubate_instead_of_counting_as_wins` |
| 激活版本与文件内容不一致 | `promote()` 优先读未版本化的 `champions/<skill>/SKILL.md`；新 Champion 不退休旧 Champion | 按 `candidates/<ver>` 或 `champions/<skill>/<ver>` 取文件；校验 frontmatter version 与 registry hash；新 Champion 将仍为 `champion` 的旧版本标为 `retired`；一致性检查比对 Champion 内容 | `test_promote_loads_the_requested_version_not_the_current_pointer`、`test_new_champion_retires_the_previous_champion`、`test_consistency_reports_champion_pointer_version_mismatch` |
| rejected + `--force` 仍把内容写上线 | 先写 Active，再在 `_finalize_activation` 里拒绝非法迁移 | 状态/哈希/版本检查全部在写盘前完成；`--force` 只放开 candidate/shadow；rejected 即使 force 也拒绝 | `test_rejected_force_promote_does_not_write_active` |
| Gate 先改状态、评测证据后落盘 | `mark_champion()` 发生在 artifact `atomic_write_text` 之前 | 先持久化带 Gate 摘要的评测产物，成功后再提交生命周期；`mark_champion` 先写版本化 Champion 文件，再改 registry，最后更新当前指针 | `test_artifact_write_failure_does_not_create_a_champion` |

脱敏补强：Unix 路径覆盖 `/etc`、`/opt` 等常见根，并识别 Windows 盘符路径与 UNC；日期增加 `11/03/2024` 这类月日年格式。

当前 P1 测试规模（第 9 节时）：`test_skill_registry.py` 23、`test_skill_gate.py` 19、`test_skill_cli.py` 5、`test_skill_shadow_eval.py` 22、`test_online_candidate_isolation.py` 8。

## 10. 审查返工：Judge 类型校验与评测入口状态限制

针对 `323d70b` 的两个阻断项，以及手工 Active Skill 被一致性检查误报的兼容问题。返工后宿主机 `187 passed`；P1 新增范围 Ruff 通过。

| 阻断项 | 根因 | 修复 | 测试 |
|--------|------|------|------|
| Judge 返回 `"false"` 字符串被当成通过 | `bool(parsed["pass"])` 把非空字符串当成 True；infrastructure 只识别抛异常 | `parse_judge_verdict()` 只接受 JSON boolean；缺字段、类型错误、非 JSON、空响应一律 `infrastructure_error`，pair 无效，Gate 只能 `incubating` | `test_judge_verdict_rejects_string_false_and_other_payloads`、`test_string_false_judge_verdict_is_infrastructure_not_a_pass`、`test_empty_and_non_json_judge_payloads_invalidate_the_pair` |
| `/skill-eval <champion\|active>` 可把线上版本打成 rejected | `run_paired_evaluation` 不限制待评状态，Gate 的 reject 分支会 `transition(..., rejected)` | 入口只接受 `candidate`/`shadow`；生命周期提交前再次核对状态 | `test_evaluating_a_champion_version_is_refused`、`test_evaluating_an_active_version_is_refused` |
| 手工 `/skill-create` 被报无 registry 记录 | 一致性检查把所有不在 registry 的 Active 文件当漂移 | 只对「已有 registry 节点但没有 `active_version`」的治理技能报警；纯手工 Active 视为非治理技能 | `test_consistency_ignores_handmade_active_skills_without_registry`、`test_consistency_reports_governed_active_file_without_active_version` |

当前 P1 测试规模：`test_skill_registry.py` 24、`test_skill_gate.py` 21、`test_skill_cli.py` 5、`test_skill_shadow_eval.py` 26、`test_online_candidate_isolation.py` 8。

