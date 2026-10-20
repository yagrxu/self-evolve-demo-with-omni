# 从 Agent 创建到用评估持续改进：纯 AWS CLI 全流程（CloudWatch GenAI Observability + AgentCore Evaluations）

> 适用对象：已经有（或准备上线）一个 AgentCore 上的 agent，想建立「数据集 → 评估 → 分类 → 改进 → 验证」闭环的团队。
> 全流程只用 **AWS CLI v2（≥ 2.38）+ CDK + jq + python3 标准库**，可以交给一个 coding agent 按脚本执行（见 `skills/agentcore-eval-loop/SKILL.md`）。
> 每条命令都在 us-west-2 实测过（2026-10-20），实测结论标注为「实测」。

---

## 0. 一张图看全流程

```
 ┌─ 准备 ───────────────┐  ┌─ 建立基线 ─────────────────────────┐  ┌─ 改进一轮 ───────────────────────────────┐
 │ 00 前置检查           │  │ 03 数据集（AgentCore Dataset）      │  │ 07 生成候选（只改一层）                   │
 │ 01 CDK 部署 baseline  │→ │ 04 baseline 跑 ≥2 个独立批次        │→ │ 01 部署候选（独立 runtime）               │
 │ 02 验证可观测性       │  │ 05 带标准答案的 batch evaluation    │  │ 04/05 候选跑同样的批次                    │
 └──────────────────────┘  │ 06 失败分类（insights）→ 决定改哪层 │  │ 09 配对比较 → 阶段门禁（S0 → S1 → S2）   │
                           │ 09 A/A 校准 → 登记门禁              │  │ 10 上线 / 回滚（需人确认）                │
                           └────────────────────────────────────┘  └──────────────────────────────────────────┘
```

每一步都有**停点**：产出一个可以审阅的文件，人确认后才进入下一步。

---

## 1. 每一步做什么、为什么、用哪些 CLI

| 步骤 | 脚本 | 核心 CLI | 为什么需要这一步 | 停点产物 |
|---|---|---|---|---|
| 00 前置检查 | `00_prereq.sh` | `xray get-trace-segment-destination` · `xray get-indexing-rules` · `bedrock list-foundation-models` | 没开 Transaction Search，span 不会进 `aws/spans`，后面所有评估都是「0 条 trace」且**不报错** | 终端输出 |
| 01 部署 | `01_deploy.sh baseline` | `cdk deploy`（Direct Code Deploy，`opentelemetry-instrument` 作 entrypoint） | ADOT 自动埋点，不改业务代码就有 trace；托管运行时不替你装依赖，依赖要 vendored 进 zip | `deploy-baseline.json` |
| 02 验证可观测性 | `02_verify_observability.sh` | `bedrock-agentcore invoke-agent-runtime` · `logs filter-log-events` | 发 1 条请求，确认 `invoke_agent / execute_tool / chat` 三类 span 都落地 | span 名称计数 + 控制台链接 |
| 03 数据集 | `03_dataset.sh control` | `bedrock-agentcore-control create-dataset` · `create-dataset-version` · `list-dataset-examples` | 标准答案来自**确定性 oracle**，并打不可变版本；评估结论引用版本号 | `dataset-control.json` + 快照 |
| 04 跑批次 | `04_traffic.sh baseline control 1` | `invoke-agent-runtime`（session id 编码 run/arm/scenario/batch） | 对齐 trace 与标准答案只靠 session id，绝不靠位置 | `traffic/*.jsonl` |
| 05 批量评估 | `05_batch_eval.sh baseline control 1` | `bedrock-agentcore start-batch-evaluation`（`evaluationMetadata.sessionMetadata[].groundTruth`）· `get-batch-evaluation` · `logs get-log-events` | 云上评估、带标准答案，逐 session 结果落在专用 log group | `results/*.jsonl`（逐 session × evaluator） |
| 06 失败分类 | `06_insights.sh baseline control 1 2` | `start-batch-evaluation --insights Builtin.Insight.FailureAnalysis / UserIntent / ExecutionSummary` | 把失败聚类成根因，每个根因带 `fixType` 和受影响 session —— 决定**这一轮改哪一层** | `classification.json` |
| 09 A/A 校准 + 登记 | `09_gates.py calibrate` / `register` | （纯本地算术） | 不知道「什么都没改时 Δ 抖多少」，门槛就是拍脑袋；登记必须在任何候选结果出现之前 | `calibration.json` · `gates.registered.S0.json` |
| 07 生成候选 | `07_recommend.sh c1 auto …` 或 `manual` | `bedrock-agentcore start-recommendation`（`SYSTEM_PROMPT_RECOMMENDATION` / `TOOL_DESCRIPTION_RECOMMENDATION`） | 基于 trace 让服务给出 prompt / 工具描述改写建议；也可以人工撰写 | `candidates/<id>/prompts.json` |
| 01 部署候选 | `01_deploy.sh c1 <prompts>` | `cdk deploy selfevolve-demo-agent-c1` | **独立 runtime + 独立 stack**，baseline 不受影响；候选只换一个文件 | 候选 runtime ARN |
| 04/05 候选批次 | 同上，arm = c1 | 同上 | 与 baseline **按批次交替**跑同一份数据集 | 同上 |
| 08 在线评估覆盖候选 | `08_online_eval.sh create <arm>` | `bedrock-agentcore-control create-online-evaluation-config`（evaluator 照抄生产 config）· `iam create-role / put-role-policy` | 生产的默认评估只绑定 baseline runtime；候选是独立 runtime，不建就**不会**被在线评估。S2 真实流量没有标准答案，在线评估是唯一评分来源 | `online-eval-<arm>.json` |
| 09 比较 | `09_gates.py compare --stage S0` | （纯本地算术） | 按 scenario 配对、多批次取均值、bootstrap CI、逐维回退、成本、safety | `gates-S0-c1-control.json` |
| 10 上线 / 回滚 | `CONFIRM=yes 10_promote.sh c1 S2` | `cdk deploy`（baseline 换成胜者 prompts） | 对外可见变更：门禁 PASS + 人确认；保留一键回滚 | `promote/*.json` |
| 99 清理 | `99_cleanup.sh` | `cloudformation delete-stack` · `delete-dataset` · `delete-batch-evaluation` | 只删本 RUN_ID 的候选与记录，不动生产 | — |

### 实测踩坑（都不报错，只会给出错误结果）

| 现象 | 原因 | 做法 |
|---|---|---|
| batch evaluation `totalNumberOfSessions: 0` | `logGroupNames` 只写了 runtime 的 log group | **必须包含 `aws/spans`**（span 在这里），再加 runtime log group |
| insight 运行「全部 session 失败」 | insight id 写成 `Builtin.FailureAnalysis` | 合法值只有 `Builtin.Insight.FailureAnalysis / UserIntent / ExecutionSummary` |
| `evaluators and insights are mutually exclusive` | 两者放进同一个 batch | 分两次跑（05 评分、06 分类） |
| 推荐失败「detected as unsafe by prompt attack protection」 | 中文 v1 prompt「……让用户满意。」被误判 | 用英文同义底稿（`BASE_PROMPT_OVERRIDE`）或 manual 模式 |
| 推荐 `evaluators` 校验失败 | Recommendation 只接受 **1 个** evaluator | 取 gates.json 第一个质量 evaluator |
| 数据集 `Required field 'scenario_id' is missing` | 用了驼峰字段 | schema 是蛇形：`scenario_id / turns[].input / expected_response / assertions / expected_trajectory` |
| token 统计翻倍 | 相邻批次时间窗口重叠 | 按 session 集合过滤（05 已处理） |

---

### 两种评估各管什么

| | 默认评估（Online Evaluation，CDK eval stack） | 批量评估（Batch Evaluation） |
|---|---|---|
| 何时打分 | 持续、自动，session 空闲 15 分钟后 | 按需，对指定 session / 时间窗 |
| 标准答案 | 无（只能判「看起来对不对」） | 有（按 session 注入 groundTruth） |
| 覆盖范围 | 只覆盖它配置的那个 runtime | 任意 runtime（按 serviceName + log group） |
| 用途 | 生产监控、诊断来源、**S2 灰度的唯一评分** | 离线实验：S0 / S1 的配对比较 |
| 本 demo | baseline 121 个 session 被覆盖；候选需用 08 单独建 | 全部 S0 / S1 结论 |

### 数据集按用途分四组

| 组 | 从哪来 | 用来做什么 | 谁能看 / 何时看 |
|---|---|---|---|
| **eligible**（合格池） | 生产 trace 中能对上标准答案的样本 | 盘点与切分来源，不直接参与决策 | — |
| **control** | eligible 中稳定哈希 ≥ 30% | 重复采样诊断、决定改哪一层、设计候选、S0 配对比较 | 可反复看 |
| **holdout** | eligible 中稳定哈希 < 30%（同分布） | S1：只测领先的一个候选 | 切分后封存，设计候选时不看 |
| **verify** | 单独构建、订单区间与前三组不相交 | S1 最终确认，抓过拟合 | 出结果前登记标准，只跑一次 |

每组注册为独立的 AgentCore Dataset 并打不可变版本；holdout 用「比例 + 最少条数」双约束。
本 CLI demo 简化了：control 直接用 prod_sim（未切 holdout），确认集用 fresh + verify（各 20 条）。

## 2. 改哪一层：从分类结果到决定

06 给出每个根因的 `fixType` 和受影响 session 数。**不要直接照单全收**：

| 看到的信号 | 更可能是哪一层 | 例子（本 demo 实测） |
|---|---|---|
| 工具返回正确，模型的结论和它矛盾 / 漏信息 / 表达不全 | **Prompt** | 破损全额退款只说「全额退款」不写金额 |
| 模型自己做日期 / 金额 / 规则计算并出错 | **Tool**（让工具直接返回结果） | 券分摊、折旧费基数、舍入；insight 会把它标成 `SYSTEM_PROMPT_FIX`，但改 prompt 只能降低错误率 |
| 选错工具、参数错 | **Tool 描述 / schema** | `ToolSelectionAccuracy` 低 |
| 必须先做的步骤被跳过（尤其不可逆动作之前） | **Workflow** | 没查政策就 `create_return_label` |
| 只在某一类任务上出错，修它需要一大段专用步骤 | **Skill** | 只影响「部分退货」一类 |
| prompt 有**明确指令**、跨多类任务、重复跑仍违反 | **Model** | 先排除 prompt 根本没写清楚 |

**普遍性**：同一个 scenario 在 k 个独立批次里失败几次。k/k 是系统性，1/k 偏向采样波动。至少 2 个批次才能区分。

**一轮只改一层**。改完重新测量，再看下一层。

> 实测（40 个 session）：insight 把全部 12 个根因都标为 `SYSTEM_PROMPT_FIX`，其中「金额计算精度」「折旧费基数」「日期计算」三类按上表其实是 Tool 层问题。Recommendation API 给出的候选 prompt 也是「把计算规则写进 prompt」，且漏掉了破损商品退原始运费的例外 —— 自动生成的候选**必须对照政策人工审阅**。

---

## 3. 门禁怎么设：动态、事先登记、逐级收紧

### 3.1 为什么不能用固定阈值
同一个阈值在不同样本量、判官噪声、业务成本下含义完全不同。本 demo 的 **A/A 实测**（同一个 baseline、两个独立批次、20 个 scenario）：

| 指标 | 两个**完全相同**的 arm 之间 |
|---|---|
| 综合质量 Δ | −0.014，95% CI **[−0.134, +0.100]** |
| token | **+10.3%** |
| 延迟中位数 | **+5.6%** |

也就是说：如果门禁写死「CI 下界 ≥ −0.05、token ≤ 10%」，**一个什么都没改的候选也会被拒**。门槛必须由噪声和业务一起决定。

### 3.2 门槛怎么算（`09_gates.py register`）

| 门 | 怎么定 | 来源 |
|---|---|---|
| 最小提升 | S0/S1：≥ 0（非劣效）；S2：max(业务最小有意义提升, 2 × A/A 噪声) | A/A + 业务 |
| CI 下界 | S0/S1：≥ −非劣效边界；S2：≥ 0 | 业务给边界 |
| 逐维回退 | ≥ −max(边界, 单格翻转分辨率 = 1/(场景数 × 批次数)) | 由样本量算出；低于分辨率的门在数学上不可达 |
| 成本 | token / 延迟 ≤ 业务预算（本 demo：15% / 10%） | 业务 |
| safety | 零新增失败；只用**通过误报测试**的判官 | 判官自一致性测试 |
| 可达性 | 若 A/A 的 CI 半宽 > 边界 → 登记时报警「同一个 prompt 也过不了」，要求加批次 / 加样本，或业务在登记前放宽 | A/A |

**登记时机**：A/A 之后、任何候选结果出现之前。脚本会检查：如果已经存在候选的结果文件，拒绝登记。

### 3.3 分阶段收紧

| 阶段 | 数据 | 样本 | 影响范围 | 问题 | 门禁 |
|---|---|---|---|---|---|
| **S0** | control | ≥ 20 scenario × ≥ 3 批次 | 无 | 值得继续吗 | 非劣效（边界 0.05） |
| **S1** | confirm（从未用于挑选） | ≥ 40 scenario | 无 | 没见过的题上不更差 | 非劣效（边界 0.03） |
| **S2** | 云上灰度（真实流量 + online evaluation） | ≥ 200 session | 部分用户 | 真的更好吗 | CI 下界 ≥ 0，成本按预算，safety 零新增 |
| **S3** | 全量 | 全部 | 全部 | 有无回退 | 持续监控 + 自动回滚 |

最少样本量按「要分辨多大的差异」反推：n ≈ (1.96 σ / 边界)²。本 demo 每个 scenario 的 Δ 标准差 σ ≈ 0.155：分辨 ±0.05 需要约 37 个，±0.03 需要约 100 个。样本不够时**等流量**，不要降门槛。

---

## 4. 考量因素清单

- **数据集**：标准答案只来自确定性规则 / 人工确认，绝不复制某次线上回答；control / confirm 订单不相交；confirm 在出结果前不看。
- **对齐**：session id 编码 scenario，所有 join 都按 id，不按位置。
- **重复**：分批独立跑（实测同一时段内的重复高度相关）。
- **判官**：读 `get-evaluator` 的真实 ratingScale 再归一化；新判官先做误报测试；一个 evaluator 被判为系统性误报（本 demo 的 `ThirdParty.AutoEval.Security`）就移到 `report_only`。
- **一次只改一层**：候选是独立 runtime，只换一个文件 / 一个开关；CDK 模板 diff 只应出现这一处。
- **对照组与候选同包**：线上 baseline 可能是很久以前打的包（代码、fixtures 都旧）。比较时用当前代码重新打包一个对照 arm（`01_deploy.sh b0 <baseline prompts>`，`09_gates.py compare --base b0`）。实测：旧包里没有确认集的订单，导致 S1 出现 +0.435 的假提升（demo §15）。
- **期望轨迹按工具层选择**：Tool 层候选会换用新工具，按旧工具层写的 `expected_trajectory` 必然判失败；登记门禁前决定该维度对哪些 arm 适用。
- **事先登记**：门禁、候选列表、确认集在出结果之前写进文件；看到结果之后的任何调整都要记录时间和理由，并标记 `calibrated_after_results`。
- **上线**：本地（S0/S1）只证明「不更差」；「更好」要在 S2 用真实流量证明。上线必须人确认，保留一键回滚。
- **成本**：每个批次 = 场景数次 agent 调用 + 场景数 × evaluator 数次判官调用；insight 与 recommendation 按 session 计费。先用 A/A 定样本量，避免盲目加批次。
