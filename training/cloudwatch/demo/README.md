# 完整演示记录：用 CloudWatch / AgentCore 工具改进当前的售后 agent（run `cwdemo1`）

- 时间：2026-10-20 15:23 – 19:30（北京时间）· region us-west-2 · account 613477150601
- 对象：已部署的 `selfevolve_demo_order_support`（AgentCore Runtime，endpoint `v1`，prompt `order_support-v1`）
- 工具：只用 `training/cloudwatch/scripts/` 下的脚本（AWS CLI v2.38.1 + CDK + jq + python3 标准库），不用 Omni
- 原始输出：`logs/`（每一步一个文件，编号与下文一致）· 关键产物：`artifacts/`
- 运行时数据（含 agent 回答，不入库）：`training/cloudwatch/runs/cwdemo1/`

## 结论先行

| 阶段 | 候选 | 对照 | 质量 Δ（95% CI） | 结局 |
|---|---|---|---|---|
| S0 control 第 1 轮 | `p1`（Prompt 层） | baseline | −0.010（−0.111, +0.094） | **FAIL** |
| S0 control 第 2 轮 | `t1`（Tool 层，prompt 不变） | baseline | +0.081（+0.001, +0.176） | 原门禁 **FAIL** → 用户校准后 **PASS_UNDER_CALIBRATED_GATES** |
| S1 confirm（第一次） | `t1` | baseline | +0.435 | **作废**：线上 baseline 的包里没有 confirm 集的订单（§15） |
| S1 confirm（有效，20 题） | `t1` | `b0`（同一份代码与数据重新打包的 baseline） | +0.161（+0.044, +0.282） | FAIL：只差样本量（20 < 40） |
| S1 confirm + verify（40 题） | `t1` | `b0` | +0.162（+0.083, +0.243） | 记录的门禁 FAIL（token +55.8% > 55%）→ 用户第二次校准（token ≤ 60%）后 **PASS_UNDER_CALIBRATED_GATES** |
| S2 云上灰度 | — | — | — | **待批准**（§20） |

**一句话**：Tool 层候选 t1 在 control（20 题 × 4 批）和 40 道从未用于挑选的确认题（× 2 批）上 CI 下界都为正，确认集 Correctness +0.312、26 题变好 / 9 题变差；代价 token +55.8%。S1 在用户两次校准后（第二次在看过结果之后）通过；「真的更好」还需 S2 用真实流量证明。生产 baseline 全程未改动，未上线。

---

## 1. 前置检查 —— `logs/00-prereq.log`
**为什么**：没开 Transaction Search，span 不会进 `aws/spans`，后面所有评估都是「0 条」且不报错。
**命令**：`bash scripts/00_prereq.sh`
**结果**：aws CLI 2.38.1（含 batch evaluation / dataset / recommendation）✓ · Transaction Search `CloudWatchLogs/ACTIVE`，采样 100% ✓ · `aws/spans` 存在 ✓ · Haiku 4.5 可用 ✓

## 2. 生成数据集 —— `logs/01-generate-dataset.log`
**为什么**：标准答案必须来自确定性规则，而不是某次线上回答。本 demo 的 oracle 是 `agent/tools.py` 里的纯函数（与 agent 运行时同一套算法）。
**命令**：`python3 scripts/build_datasets.py`
**结果**：4 份数据集（dev_local 15 / prod_sim 20 / verify 20 / fresh 20），订单区间两两不相交 ✓；重新生成与仓库中已有文件逐字节一致（`git status datasets/` 无变化）。
本 demo 用 `prod_sim` 作 **control**、`fresh`（从未被任何 run 用来挑选候选）作 **confirm**。

## 3. 核对已部署的 baseline —— `logs/02-baseline-deploy-check.log`
**为什么**：baseline 是生产，不重新部署；但要确认「训练脚本合成的 baseline 模板」与线上一致。
**命令**：`describe-stacks` · `get-agent-runtime` · `01_deploy.sh baseline --synth-only`
**结果**：runtime READY，version 1，entrypoint `opentelemetry-instrument agent.py`。
**发现并修复的问题**：第一次合成时 `01_deploy.sh` 把 `AGENT_PROMPT_VARIANT` 覆盖成了 `baseline`（线上是 `v1-baseline`）—— 如果真部署，会改动线上 runtime 的环境变量。已修，修后模板与线上一致（忽略 asset hash）。

## 4. 验证可观测性 —— `logs/03-verify-observability.log`
**为什么**：确认 span 真的进了 CloudWatch。
**命令**：`02_verify_observability.sh baseline`（1 次 `invoke-agent-runtime` + `logs filter-log-events`）
**结果**：13 个 span：`AgentCore.Runtime.Invoke` · `invoke_agent Strands Agents` · `chat` ×4 · `execute_tool get_order / get_refund_policy` · …
**截图**：CloudWatch → GenAI Observability → Bedrock AgentCore → `selfevolve_demo_order_support` → session `cwt-cwdemo1-baseline-smoke-b0-0-0`

## 5. 注册 AgentCore Dataset —— `logs/04-dataset-*.log` · `artifacts/dataset-*.json`
**命令**：`03_dataset.sh control` / `confirm`（`create-dataset` → `get-dataset` 读回 → `create-dataset-version` → `list-dataset-examples` 快照）
**结果**：

| 角色 | dataset id | 条数 | 版本 |
|---|---|---:|---|
| control | `cwdemo1_control-EQgQ179Q3o` | 20 | 1 |
| confirm | `cwdemo1_confirm-7Xh65nH8MX` | 20 | 1 |

云上 scenario_id 集合与文件一致 ✓。

## 6. baseline 跑 4 个独立批次 + 带标准答案的批量评估 —— `logs/05-*.log` · `logs/06-*.log`
**为什么**：每个 scenario 只跑 1 次分不清系统性失败和偶发失败；同一时段内的重复又高度相关，所以分 **4 个独立批次**。
**命令**：`04_traffic.sh baseline control 1..4` → `05_batch_eval.sh baseline control 1..4`（`start-batch-evaluation`，每个 session 注入 expected_response / assertions / expected_trajectory）
**结果**：80/80 次调用成功；批量评估 79/80 个 session 完成（b4 有 1 个 `COMPLETED_WITH_ERRORS`）。

| 批次 | Correctness | Helpfulness | GoalSuccessRate | TrajectoryInOrder | PIILeakage |
|---|---:|---:|---:|---:|---:|
| b1 | 0.75 | 0.79 | 0.45 | 0.75 | 0.95 |
| b2 | 0.65 | 0.81 | 0.40 | 0.75 | 0.97 |
| b3 | 0.65 | 0.80 | 0.35 | 0.65 | 1.00 |
| b4 | 0.80 | 0.78 | 0.42 | 0.89 | 0.99 |

同一个 prompt，Correctness 在批次之间从 0.65 摆到 0.80 —— 这就是后面门禁要面对的噪声。

**普遍性**（`logs/07b-prevalence.log`）：Correctness 在 4 个批次里失败 ≥3 次的**系统性** scenario 有 5 个：prod_005 · prod_008 · prod_013 · prod_014 · prod_020；偶发（1/4）6 个；从不失败 8 个。

## 7. 失败分类 —— `logs/07-insights-baseline-control.log` · `artifacts/insights-classification.json`
**命令**：`06_insights.sh baseline control 1 2 3 4`（`Builtin.Insight.FailureAnalysis / UserIntent / ExecutionSummary`，80 个 session）
**结果**：修复层分布 `SYSTEM_PROMPT_FIX` 26 · `TOOL_DESCRIPTION_FIX` 1。前 5 个根因：

| 根因 | session | 主要 scenario |
|---|---:|---|
| Reasoning-Action Contradiction in Return Eligibility | 6 | prod_005 4/4 |
| Premature Return Action Without Policy Verification | 4 | prod_005 2/4、prod_014 |
| Incorrect Return-Type Classification in Shipping Refund Logic | 4 | prod_001 2/4 |
| Internal Consistency Failures in Eligibility Reasoning | 3 | — |
| Policy Window Boundary Conflation | 3 | prod_008 3/4 |

**修复的脚本问题**：insight 的 `affectedSessions` 里同一个 session 会因多个失败 span 重复出现（出现过 "prod_005 6/4"），已按 session 去重。另：调试时误触发了一次重复的 insight 分析（`…_173719`），本记录采用第一次（`…_173254`）的结果。

**层的决定（第 1 轮）**：系统性失败里 prod_005 / prod_008（超窗口仍暗示可退、前后矛盾）和 prod_013 / prod_020（结论对但不写金额）是工具数据正确、模型判断 / 表达出错 → **Prompt 层**；prod_014（先开退货单再查政策）是 Workflow 层，本轮不处理；算术类错误都是 1/4 偶发，属 Tool 层，本轮不碰。

## 8. A/A 校准 + 登记门禁 —— `logs/08-*.log` · `artifacts/calibration.json` · `artifacts/gates.registered.S0.json`
**为什么**：不知道「什么都没改时 Δ 抖多少」，门槛就是拍脑袋；登记必须早于任何候选结果。
**命令**：`09_gates.py calibrate --a-batches 1 2 --b-batches 3 4` → `register --stage S0`
**A/A 结果**（baseline {1,2} vs {3,4}，20 scenario）：Δ −0.014，95% CI **[−0.111, +0.076]**，噪声 0.093。逐维：Correctness ±0.158、TrajectoryInOrder ±0.188、GoalSuccessRate ±0.138、Helpfulness ±0.050 —— 噪声主要来自 agent 自身跨批次的行为波动。
**登记前的决定**（`gates.demo.json`，记录为 demo operator 决定、待评审）：模板的非劣效边界 0.05 在这个样本量下不可达（需约 3.5× 样本），本 demo 把 S0/S1 定位为「筛选：没有明显变差」，**边界 0.10**；「真的更好」留给 S2。
**登记的 S0 门禁**：均值 ≥ 0 · CI 下界 ≥ −0.10 · 最差单维 ≥ −0.10 · 延迟 ≤ +10% · token ≤ +15% · safety 新增 = 0 · 样本 ≥ 20。登记时刻 09:41:39Z，早于任何候选。
S1 门禁同样在任何 confirm 结果之前登记（09:46:01Z）。**登记时的疏漏**：S1 要求 ≥ 40 个 scenario，而 confirm 集只有 20 个 —— 按规则登记后不改，这一点如实保留。

## 9. 第 1 轮：Prompt 层候选 p1 —— `logs/09-*.log` ~ `logs/15*.log` · `artifacts/candidates/p1/`
**候选**（`07_recommend.sh p1 manual`，在 v1 后追加两句）：
> 先依据工具返回的订单和政策判定资格，只给一个结论，前后不得矛盾；超出退货窗口时直接说明不能退款，只提供政策允许的换新或保修路径，不暗示例外。可以退款时写出每件可退金额、原始运费是否退还及退款合计（元）。

脚本断言：除 system prompt 与新增 history 外，与 baseline 逐键一致。
**部署**：`01_deploy.sh p1 …` → 独立 stack `selfevolve-demo-agent-p1`、runtime `selfevolve_demo_order_support_p1-C0MWWUH3sd`（146 s）。baseline 不受影响。
**跑批次**：同一份 control，4 个批次，80/80 成功，评估 80/80。
**门禁（S0）**：

| 门 | 值 | 门槛 | |
|---|---:|---|---|
| 均值提升 | −0.010 | ≥ 0 | ✗ |
| CI 下界 | −0.111 | ≥ −0.10 | ✗ |
| 最差单维 | −0.073（Helpfulness） | ≥ −0.10 | ✓ |
| 延迟 | −7.3% | ≤ 10% | ✓ |
| token | −5.9% | ≤ 15% | ✓ |
| safety 新增 | 0 | = 0 | ✓ |

**逐 scenario 看**（`logs/15b-p1-per-scenario.log`）：目标命中 —— prod_008 3/4→0/4、prod_014 3/4→0/4、prod_005 3→1、prod_013 3→1；但「写出金额」让算术错误暴露 —— prod_001 0→3、prod_018 1→3、prod_010 0→2。Correctness 失败总数 23 → 23，Helpfulness 失败 3 → 13。
**解读**：和 Omni 那次 run 一样的**跨层副作用**：Prompt 要求把数字写清楚，Tool 层「靠模型心算」的缺陷就直接暴露给用户。→ 第 2 轮改 Tool 层。

## 10. 第 2 轮：Tool 层候选 t1 —— `logs/16-*.log` ~ `logs/20*.log`
**候选**：只改工具层 `AGENT_TOOL_TIER=enhanced`（新增 `check_return_eligibility` / `calculate_refund` / `get_coupon_allocation`，由工具直接返回资格与金额），prompt 保持 `order_support-v1`（hash `c76023c4…`）。合成确认 runtime 环境变量只多了 `AGENT_TOOL_TIER=enhanced`。
**部署**：stack `selfevolve-demo-agent-t1`、runtime `selfevolve_demo_order_support_t1-pOfuBW665N`。可观测性验证里看到了新工具的 span（`execute_tool calculate_refund` / `check_return_eligibility`）。
**跑批次**：4 个批次，80/80 成功，评估 80/80。
**门禁（S0，用同一份事先登记的门禁）**：

| 门 | 值 | 门槛 | |
|---|---:|---|---|
| 均值提升 | **+0.081** | ≥ 0 | ✓ |
| CI 下界 | **+0.001** | ≥ −0.10 | ✓ |
| 最差单维 | **−0.675**（TrajectoryInOrderMatch） | ≥ −0.10 | ✗ |
| 延迟 | −2.2% | ≤ 10% | ✓ |
| token | **+50.5%** | ≤ 15% | ✗ |
| safety 新增 | **1**（prod_015 · PIILeakage） | = 0 | ✗ |

逐维 Δ：Correctness **+0.130** · Helpfulness +0.049 · GoalSuccessRate +0.062 · InstructionFollowing +0.017 · TrajectoryInOrderMatch −0.675
Correctness 失败 23 → **13**；系统性的 prod_005 / prod_008 / prod_014 全部 → 0/4；仍未解决：prod_013 3/4、prod_020 3/4、prod_017 3/4、prod_004 1→3。

**三道没过的门，逐一看证据**（`logs/20b-t1-analysis.log`）：
1. **轨迹 −0.675 是测量设计问题**：数据集的 `expected_trajectory` 是按 basic 工具层写的 `[get_order, get_refund_policy]`；t1 走的是 `get_order → check_return_eligibility`，判词原文 "Tools ['get_refund_policy'] not found in order"。换了工具层，旧的期望轨迹就不再适用 —— 这道守门维度对 Tool 层候选不成立（数据集 metadata 里其实有 `expected_trajectory_enhanced`，但门禁登记时没有按工具层区分）。
2. **token +50.5% 是真实成本**：多了工具调用、回答里有逐项金额。是否接受是业务决定。
3. **PII 的 1 个新增**：`ThirdParty.DeepEval.PIILeakage` 把「订单号、优惠券、退款金额」判成隐私泄露（4 次里 3 次给 0 分）；回答越详细越容易被判。这与 Security 判官的情况类似，属于判官对本场景的误报倾向，需要做误报测试后再决定是否保留为硬门。

## 11. 第 2 轮结束时的状态（当时）
**结局（按登记的门禁）**：第 1 轮 p1 **FAIL**，第 2 轮 t1 **FAIL** → 本 run `NO_CHANGE`，不跑 S1，不上线。生产 baseline 全程未改动。

t1 是这一轮最有价值的结果：它是**唯一一个 CI 下界为正**的候选，Correctness 失败数减少 43%，而且是在 prompt 完全不变的前提下（证明问题确实在 Tool 层）。它没过的门里：
- 轨迹维度 —— 测量设计问题，应在登记前按工具层选择期望轨迹；
- PII —— 疑似判官误报，需要误报测试；
- token +50% —— 真实成本，需要业务判断。

当时给出的选项（用户选择了 2）：
1. **按 SKILL 规则开新一轮**（推荐）：新 RUN_ID，在登记前修正测量设计（按工具层选期望轨迹、PII 判官先做误报测试、由你给 token 预算），再测 t1 → 过 S0 后用 confirm 集做 S1。
2. **在本 run 记录一次门禁校准**（标记 `calibrated_after_results`）：去掉轨迹守门维度、PII 记为误报、接受 token +50%，然后进入 S1。
3. 维持 `NO_CHANGE`，清理候选资源。

## 12. 本次跑出来的脚本 / 方法论问题（已修或待修）
| # | 问题 | 状态 |
|---|---|---|
| 1 | `01_deploy.sh baseline` 会把 `AGENT_PROMPT_VARIANT` 改掉，重新部署时改动线上 env | 已修 |
| 2 | `02_verify_observability.sh` 首次轮询就报 span 数（不全） | 已修：等待后统计，并断言三类 span 都在 |
| 3 | insight 汇总按 span 重复计 session | 已修：按 session 去重；新增 `SUMMARIZE_ONLY=1` 只重算汇总 |
| 4 | 期望轨迹没有按工具层区分，Tool 层候选必然在轨迹维度上"回退" | 待修：`05_batch_eval.sh` 按 arm 的工具层选 `expected_trajectory` / `expected_trajectory_enhanced` |
| 5 | S1 最少样本 40 > confirm 集 20 | 待修：confirm 集扩到 ≥ 40，或在登记前把 holdout 并入 |
| 6 | 候选包里含本地已改的 `prompt_loader.py`（多打一个 span 属性）与 `orders.json`（多了 ORD-103xx），线上 baseline 是 9 月的包 | 已知：两者都不影响 control 上的行为，但严格来说不是「只改一层」；正式运行时 baseline 也应用同一份代码重新打包 |

## 13. 云上资源（本 demo 创建）
| 资源 | 名称 / id | 清理 |
|---|---|---|
| CloudFormation stack | `selfevolve-demo-agent-p1`、`-t1`、`-b0`（各含 1 个 runtime、1 个 endpoint、1 个 IAM role） | `CONFIRM=yes RUN_ID=cwdemo1 bash scripts/99_cleanup.sh` |
| AgentCore dataset | `cwdemo1_control-EQgQ179Q3o`、`cwdemo1_confirm-7Xh65nH8MX` | 同上 |
| batch evaluation | `cwdemo1_*` 24 个（22 次评分 + 2 次 insight） | 同上 |
| online evaluation config + IAM role | `cwdemo1_b0_online`、`cwdemo1_t1_online` 及各自执行角色 | `99_cleanup.sh`（已包含） |

## 14. 门禁校准（用户决定）—— `logs/21-*.log` · `artifacts/gates.calibration.S0.json` / `S1.json`
**用户选择**：在本 run 记录一次门禁校准，接受 t1 的三项未通过，进入 S1。
**命令**：`09_gates.py calibrate-gates --stage S0|S1 --by user --reason … --exclude-dim Builtin.TrajectoryInOrderMatch --safety-report-only ThirdParty.DeepEval.PIILeakage --token-pct-max 55`
**记录内容**：理由原文、时间、`calibrated_after_results`（S0 = true，看过结果；S1 = false，在任何 confirm 结果之前沿用）、看过哪些结果文件。登记文件本身不改；`compare` 叠加校准后结局记为 `PASS_UNDER_CALIBRATED_GATES`，与 `PASS` 区分。
**S0 重算**（数据不变）：t1 → `PASS_UNDER_CALIBRATED_GATES`；原 FAIL 结果保留在 `artifacts/gates-S0-t1-control.json`，校准后的在 `…calibrated.json`。

## 15. S1 第一次比较作废 —— `logs/22-*.log` ~ `logs/25-*.log`
**跑了什么**：confirm 集（fresh，20 题），baseline 与 t1 **交替**各跑 2 个批次，先登记后比较。
**看到的结果**：Δ +0.435、Correctness +0.775 —— 大得不合理。
**原因**：线上 baseline 是 2026-09-18 的包，它的 `fixtures/orders.json` 里没有 ORD-103xx（confirm 集的订单 2026-10-19 才加入）；t1 是这次重新打的包，有这些订单。baseline 在 confirm 上 40 次回答里 **30 次是「系统查询不到订单号」**。比较的是数据层，不是工具层。
**处理**：结果改名为 `gates-S1-t1-confirm.INVALID.json`，不作为结论。

## 16. 重新打包的对照 arm `b0` + 有效的 S1 —— `logs/26-*.log` ~ `logs/30-*.log`
**为什么**：对照组必须与候选跑在**同一份代码和数据包**上，否则「只改一层」不成立。
**做法**：`01_deploy.sh b0 baseline/prompts.json`（prompt v1、basic 工具层，与线上 baseline 唯一的区别是用当前代码 / fixtures 重新打包）→ 独立 runtime `selfevolve_demo_order_support_b0-apsocPA1AM`。b0 在 confirm 上 0 次「查询不到订单」。
`09_gates.py compare` 新增 `--base`，用 b0 作对照。
**S1 结果**（t1 vs b0，confirm × 2 批次，事先登记的 S1 门禁 + 事先沿用的校准）：

| 门 | 值 | 门槛 | |
|---|---:|---|---|
| 样本量 | 20 | ≥ 40 | ✗ |
| 均值提升 | **+0.161** | ≥ 0 | ✓ |
| CI 下界 | **+0.044** | ≥ −0.10 | ✓ |
| 最差单维 | +0.025 | ≥ −0.10 | ✓ |
| 延迟 | −3.2% | ≤ 10% | ✓ |
| token | +52.0% | ≤ 55%（校准后） | ✓ |
| safety 新增（硬门） | 0 | = 0 | ✓ |

逐维 Δ：Correctness **+0.275** · GoalSuccessRate +0.150 · Helpfulness +0.058 · InstructionFollowing +0.025（TrajectoryInOrderMatch −0.775，已按校准不计入）· PII 1 次仅报告（frs_015）。13 题变好 / 5 题变差。

## 17. 最终状态与下一步
- **结局**：S0 `PASS_UNDER_CALIBRATED_GATES`；S1 **FAIL（仅样本量）**。按规则不进入上线。
- **证据强度**：两份互不相交的题集上 CI 下界都为正（control +0.001、confirm +0.044），而且 prompt 完全没变 —— 改进可以归因到 Tool 层。不足在于 confirm 只有 20 题，低于 S1 自己定的 40 题下限。
- **需要你决定**：
  1. 扩充 confirm 到 ≥ 40 题（例如并入 verify 集的 20 题，它也没被本 run 用来挑选），只补跑缺的部分再比较 S1；
  2. 接受 20 题作为 S1 样本（记录为又一次校准），进入 S2 云上灰度；
  3. 维持现状并清理资源。
- **上线（10_promote）不会自动发生**：即使 S1 通过，按分阶段门禁还需要 S2（真实流量）再证明一次，并由你确认。

## 18. 补充的方法论问题（写进 skill / 脚本）
| # | 问题 | 处理 |
|---|---|---|
| 7 | 对照组用的是旧包（线上 baseline），候选用新包 → 数据层不同步，结果作废 | 规则：**对照 arm 必须与候选同包重建**（本 run 用 b0）；`compare --base` 已支持 |
| 8 | S0 也是线上 baseline（旧包）vs 新包 | control 的订单在两个包里都有、`prompt_loader.py` 的差异只多一个 span 属性，判断为不影响结论；严格做法是 S0 也用 b0 |
| 9 | 人工校准需要可审计 | `09_gates.py calibrate-gates`：记录理由 / 时间 / 是否看过结果，结局单独标记 |

## 19. 扩充确认集到 40 题后的 S1 —— `logs/31-*.log` ~ `logs/34-*.log` · `artifacts/gates.expansion.S1.json`
**用户决定**：并入 verify 集 20 题（ORD-102xx），本 run 从未用于诊断或挑选。
**先记录再跑**：`09_gates.py expand-set --stage S1 --add confirm2 …` —— 脚本检查被扩充的集合此时还没有任何结果；`compare --set confirm,confirm2` 只接受事先记录过的扩充。
**数据集**：`03_dataset.sh confirm2` → `cwdemo1_confirm2-UQESIz4qva`（20 条，v1）。
**补跑**：b0 与 t1 在 verify 上交替各 2 批（80/80 成功，评估 80/80）。fresh 上已有的 2 批不重跑。
**S1（40 题，t1 vs b0，门禁与校准都不变）**：

| 门 | 值 | 门槛 | |
|---|---:|---|---|
| 样本量 | 40 | ≥ 40 | ✓ |
| 均值提升 | **+0.162** | ≥ 0 | ✓ |
| CI 下界 | **+0.083** | ≥ −0.10 | ✓ |
| 最差单维 | +0.013 | ≥ −0.10 | ✓ |
| 延迟 | −3.6% | ≤ 10% | ✓ |
| token | **+55.8%** | ≤ 55%（校准后） | **✗**（超 0.8 个百分点） |
| safety 新增（硬门） | 0 | = 0 | ✓ |

逐维 Δ：Correctness **+0.312** · GoalSuccessRate +0.113 · Helpfulness +0.062 · InstructionFollowing +0.013；PII 2 次仅报告（frs_015、vfy_018）。26 题变好 / 9 题变差。

**结局**：S1 按记录的门禁 **FAIL**（只差 token 预算）。我没有再调整阈值 —— 55% 是在 confirm 结果出现之前沿用的校准值，现在改就是看了结果再定标准。

**需要你决定**：
1. 接受 token +55.8%（记为又一次校准，`calibrated_after_results: true`），S1 记为 `PASS_UNDER_CALIBRATED_GATES`，进入 S2 云上灰度；
2. 维持 FAIL，下一轮先降 token（例如让 `calculate_refund` 直接返回可读的明细文本、减少一次模型轮次），再重测；
3. 停止并清理。

## 20. S1 第二次校准与当前状态 —— `logs/35-*.log` · `artifacts/gates.calibration.S1.json`
**用户决定**：接受 token +55.8%，预算调为 60%。
**命令**：`09_gates.py calibrate-gates --stage S1 --amend …`（`--amend` 保留上一次校准的完整历史；`calibrated_after_results` 现为 true）
**S1 重算**（数据不变）：t1 vs b0（40 题 × 2 批）→ **`PASS_UNDER_CALIBRATED_GATES`**。

**校准汇总**（全部可在 `artifacts/gates.calibration.*.json` 审计）：

| # | 阶段 | 内容 | 看过结果之后？ |
|---|---|---|---|
| 0 | S0/S1 | 非劣效边界 0.05 → 0.10（A/A 显示 0.05 不可达） | 否（登记前，demo operator） |
| 1 | S0 | 轨迹维度不计入、PII 仅报告、token ≤ 55% | 是 |
| 2 | S1 | 与 #1 相同 | 否（confirm 结果出现前沿用） |
| 3 | S1 | 扩充确认集 20 → 40 题 | 否（verify 结果出现前记录） |
| 4 | S1 | token ≤ 60% | 是 |

**S2 尚未开始**：需要你批准（见对话中的方案）。

## 21. 给候选配置默认评估（Online Evaluation）—— `logs/36-*.log` ~ `logs/38-*.log`
**为什么**：生产的默认评估 `selfevolve_demo_online_eval` 只绑定 baseline runtime —— 本 run 期间它覆盖了 baseline 的 **121** 个 session（control 80 + confirm 40 + smoke 1），而 p1 / t1 / b0 是 **0**。S2（真实流量、没有标准答案）唯一的评分来源就是在线评估，所以要先让对照与候选都被同一套 evaluator 覆盖。
**命令**：`08_online_eval.sh create b0` / `create t1`（先 `--dry-run` 审阅请求）
- 每个 arm 一个执行角色 `selfevolve-demo-online-eval-cwdemo1-<arm>`：只读该 runtime 的 log group + `aws/spans`，只写 `/aws/bedrock-agentcore/evaluations/*`
- evaluator 列表**照抄生产 config**（8 个：Correctness / InstructionFollowing / Helpfulness / Faithfulness / ToolSelection / ToolParameter / GoalSuccessRate / PolicyGrounding）
- 结果：`cwdemo1_b0_online-Dwy3m663if`、`cwdemo1_t1_online-u1h5Fx3mCv`，均 ACTIVE / ENABLED
**验证**：各发 1 个 smoke session，约 20 分钟后两边都拿到 8 个 evaluator 的在线评分（`logs/38-online-eval-smoke.log`）。
**下一步**：S2 —— 向 b0 / t1 交替打流量，用 `08_online_eval.sh export` 导出在线评分，按 S2 门禁（CI 下界 ≥ 0）比较。需要你批准后再跑。
