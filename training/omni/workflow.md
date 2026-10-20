# 从 Agent 创建到用评估持续改进：基于 CloudWatch Omni（IDE 扩展）的全流程

> 与 `training/cloudwatch/` 是同一套方法论，区别在于**实验在本地 IDE 里做**：Omni 的 MCP 工具负责本地 dev server、本地 trace、评估与数据集，云端 trace 用 `search_agent_traces` 查询。
> 本流程在本仓库真实跑过一遍（run `strands_agents-20261019T125024Z`），下文的数字都来自那次运行。
> 执行方式：`bash training/omni/scripts/run_step.sh <步骤号>`，每步跑完就停，等人确认。

---

## 0. 前提

| 项 | 要求 | 检查方式 |
|---|---|---|
| IDE | Kiro 打开本仓库，Amazon CloudWatch Omni 扩展为本目录运行 | `.omni/mcp-port` 存在 |
| 云端查询 | Kiro 进程带 `AWS_REGION` 启动（MCP proxy 不转发环境变量） | `check_credentials` 不再提示 "Cloud is not configured" |
| MCP 握手 | 扩展 ≥ 1.1.0 需要 token：`scripts/omni.mjs` 自动读 `.omni/mcp-token` | 调用不报 `Unauthorized` |
| 本地 agent | 启动命令带 `opentelemetry-instrument`，并钉住 `AWS_REGION` | Step 1 检查 span 上的 prompt 版本 / hash |
| 生产 | agent 已部署到 AgentCore，在线评估已配置（`cdk/` 的 agent + eval stack） | Step 4 读在线评估结果 |

---

## 1. 步骤

| # | 步骤 | 用到的 Omni MCP 工具 / 脚本 | 为什么 | 停点产物 |
|---|---|---|---|---|
| 1 | Preflight | `check_credentials` · `manage_test_agent(ping, invoke_agent)` · `search_local_telemetry` | prompt 由 `prompts.json` 管理、span 带 `llm.prompt_template.version / hash`，否则结果无法归因 | 终端输出 |
| 2 | 全层状态快照 | `omni_steps.py snapshot` | 记录 policy / tool / workflow / skill / prompt / model 每一层的指纹；**不在这里决定改哪一层** | `manifest.json` |
| 3 | 拉生产 trace（只读） | `discover_agent_traces_metadata` · `search_agent_traces` | 最近 24h 该 agent 的全部 span | `traces/raw.jsonl` |
| 4 | 检查生产评估结果 | 读在线评估 log group · `manage_evaluations(get_evaluator)` · `omni_steps.py ruler` | 生产标尺在 run 之前就定了；这里查覆盖率、值域、缺口，并冻结本地 replay 的标尺 | `production_eval/check.json` · `evaluators.json`（只读） |
| 5 | 切分 | `se_step6.py` | control（诊断 + 挑候选）/ holdout / fresh（从未被看过）；切分是 `scenario_id` 的纯函数 | `datasets/split.json` |
| 6 | 基线重复采样 + 按层诊断 | `manage_test_agent(invoke_agent)` × 3 遍 · `manage_evaluations(run, targets)` · `se_diagnose.py` | 生产每个 scenario 只有 1 个样本；重复 3 遍才能分系统性 vs 偶发；逐条读工具返回与判词，按层归因 | `diagnosis/summary.json` · `attribution.json` |
| 6b | A/A 校准 + 登记门禁 | baseline 再跑一组 · `gates_register.py` | 测「什么都没改时」的噪声；门槛据此计算，**早于任何候选**登记 | `gates.registered.S0.json` |
| 7 | 生成候选（只改一层） | `omni_steps.py candidate` | 完整独立的 `prompts.json`；除 prompt 外逐键一致；禁止订单号、标准答案、越层的工具名 | `candidates/<id>/` |
| 8 | paired replay | `local_server` · `manage_test_agent(set_start_command)` · `OMNI_PROMPTS_OVERRIDE` | 同时段、同顺序、同题目；每格核对 trace 上的版本 | `runs/<arm>/invocations.jsonl` |
| 9 | 门禁 S0 | `se_gates.py --registered` | 配对 bootstrap、逐维回退、成本、safety —— 全部脚本算术 | `gates-control.json` |
| 10 | 确认 S1（fresh） | 先登记 S1，再跑 baseline + 领先候选 | 只测一个候选、只跑一次 | `gates-fresh.json` |
| 11 | 决定 / 写回 | `omni_steps.py decide` | 防篡改 + 其余层未变；只写本地，不部署 | `decision.json` |

Step 6 的「由人选定改哪一层」与 `training/cloudwatch/workflow.md §2` 的判据表相同。

---

## 2. 这次真实运行得到的数字（用于讲解）

| 主题 | 结果 |
|---|---|
| 生产在线评估 | 35/35 session 覆盖；生产**没有**任何 safety evaluator |
| 旧标尺的错误 | Helpfulness 值域被记成 [0,6]（实为 [0,1]）；Harmfulness 方向记反；旧 gates.py 没有 safety 门 |
| 诊断（control 28 × 3） | 32 个失败格：Prompt 14 · Tool 12 · Workflow 6；系统性失败 5 个 scenario |
| 判官重测 | 同一批输出重评一次：候选 Δ 从 +0.023 变为 +0.050（判官噪声与 +0.03 门槛同量级） |
| 重复的独立性 | holdout dev_011：一次 3/3 算错（969.10），另一次 3/3 算对（989.10）—— 同时段内的重复高度相关 |
| A/A（本地，28 × 3） | Δ −0.020，95% CI [−0.068, +0.023]；延迟 +3.4%，token −1.0% |
| 最终（fresh，从未被看过） | 正确合计金额 19/39 → **29/39**，Correctness +0.167；token +30% |
| 安全判官 | `ThirdParty.AutoEval.Security` 对引用政策的回复**系统性误报**（两次打分 98% 一致）→ 改为 report_only |

---

## 3. 需要的 skill

| skill | 位置 | 作用 |
|---|---|---|
| `omni-self-evolution`（v2 草案） | `training/omni/skills/omni-self-evolution-v2/SKILL.md` | 本流程的 SOP：11 步 + 停点 + 不变量 + 分阶段门禁 |
| `omni-change-surface` | `.claude/skills/omni/change-surface/SKILL.md` | 层的判据（Policy → Tool → Workflow → Skill → Prompt → Model）；本次运行发现它也需修订（见 v2 草案的说明） |
| `omni-prompt-setup` / `omni-instrumentation` | `.claude/skills/omni/` | 前提：prompt 交给 Omni 管理、埋点 |
| `omni-trace-analysis` | `.claude/skills/omni/trace-analysis/` | 逐条读 trace（Step 6） |

v2 草案吸收了 `.omni/self-evolution/strands_agents-20261019T125024Z/skill-notes.md` 的 18 条修订意见；正式的 `SKILL.md` 尚未修改，等评审。

---

## 4. 门禁设置与考量因素

与 `training/cloudwatch/workflow.md §3–§4` 相同：
- 门槛由 **A/A 噪声 + 样本量分辨率 + 业务预算**决定，在任何候选结果出现之前登记（`gates_register.py` 会拒绝在已有候选分数时登记）；
- 分阶段收紧：S0 control（非劣效筛选）→ S1 fresh（没见过的题上不更差，≥ 40 scenario）→ S2 云上灰度（真的更好）→ S3 全量监控；
- 每格判官评 ≥ 3 次、重复分批独立跑，是下一轮应当先做的测量改进；
- Omni 特有：本地 dev server 的 SESSION 级 evaluator 只有 1 个有效观测（所有调用共享一个 collector session），不进门禁；replay 要在 UI 里定位某条 trace 时用 trace ID，而不是自定义 session 名。
