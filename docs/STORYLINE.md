# Agent 自我进化：从生产 trace 到可审计的 prompt 决策

> 演示故事线 · 基于 CloudWatch Omni + Bedrock AgentCore 的实测运行
> 每个 `##` 是一张 slide；`讲稿：` 是口头补充；`——` 分隔要点。
> 配套材料：[BUILD-LOG.md](BUILD-LOG.md)（完整施工日志）· [RUNBOOK.md](RUNBOOK.md)（调用流程）·
> [SKILL.md](../.kiro/skills/omni-self-evolution/SKILL.md)（方法论 SOP）

---

# 第一部分 · 技术背景

## 1. 一句话问题

**部署到生产的 agent 效果不够好，能不能让它自己变好 —— 而且是可信地变好？**

讲稿：
—— "自己变好"不难，难的是**可信**。让 LLM 改自己的 prompt、再让 LLM 判断改得好不好，
   最容易得到的是"它在你盯着的那批数据上看起来变好了"。
—— 本 demo 要证明的不是"能优化"，而是"能优化、且能**拒绝**一个假的优化"。

## 2. 三个必须同时成立的技术前提

| 前提 | 没有它会怎样 | 本 demo 怎么满足 |
|---|---|---|
| **可观测** | 不知道 agent 在生产里哪里做错了 | Bedrock AgentCore + ADOT 自动埋点 → X-Ray/CloudWatch |
| **可评估** | "变好"只是主观感觉 | CloudWatch Omni 托管 evaluator + 自定义 LLM-judge |
| **可复现** | 今天的提升明天复现不出来 | 冻结 prompt hash / dataset 版本 / 评分语义 / 固定基准日 |

讲稿：这三条缺一不可。大多数"AI 自动优化"的 demo 只做了第一条，把第二三条留给了"感觉"。

## 3. 技术栈

```
业务 agent    Strands Agents + BedrockAgentCoreApp（Python 3.12）
模型          Claude Haiku 4.5（故意用弱模型当起点）
部署          AgentCore Runtime · Direct Code Deploy（S3 zip，无 Docker）
埋点          ADOT（opentelemetry-instrument）→ X-Ray → CloudWatch Logs
评估          CloudWatch Omni（16 个 MCP 工具 + 托管 evaluator）
基础设施      AWS CDK（L2 construct，Runtime + Evaluator + OnlineEvaluation 全声明式）
region        us-west-2（与 Omni Space 同 region —— 否则 trace 查询静默返回 0 行）
```

讲稿：
—— 起点是**故意做弱的** agent：system prompt 只有一句"友好、简洁地回答，让用户满意"。
—— 弱在哪是设计好的：没要求查政策、没给工具协议、没给算术规则、没要求"不可退时给替代方案"。
   每个缺陷对应一个能被 evaluator 抓住的失败模式。

## 4. 场景：电商订单售后客服

—— 5 个商品类目 × 不同退货窗口/折旧费/运费规则 + 3 条横切规则（优惠券分摊、运费退还、破损覆盖）
—— 55 个订单，切成**三段互不相交**的 dataset：
   - `dev_local`（15）：本地基线
   - `prod_sim`（20）：云上流量模拟
   - `verify`（20）：**最终 held-out 验证，全程不许被看**
—— 招牌难题 `ORD-10012`：只退一个手机壳 → 正确答案 **¥44.88**，需要先按原价比例分摊优惠券、
   再按件拆分、再扣手续费。没有工具的模型算不对。

讲稿：dataset 的 oracle 直接用 agent 的纯函数渲染 —— 手写标注在优惠券分摊这种算术上几乎必错，
而错的标注答案会让 evaluator 把对的回答判成错的，那是最恶劣的评估污染。

---

# 第二部分 · 大循环轮廓

## 5. 六个 Phase，一个闭环

```
        ┌─────────────────────────────────────────────────────────┐
        │                                                         │
   ┌────▼─────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐      │
   │ Phase 1  │   │ Phase 2  │   │ Phase 3  │   │ Phase 4  │      │
   │ v1 agent │──▶│ 本地基线 │──▶│ 云上部署 │──▶│ 导出差   │      │
   │ + 埋点   │   │ 多维评分 │   │ 流量+评估│   │ trace    │      │
   └──────────┘   └──────────┘   └──────────┘   └────┬─────┘      │
                                                     │            │
   ┌──────────┐   ┌──────────────────────────────────▼─────┐      │
   │ Phase 6  │◀──│ Phase 5                                │      │
   │ 重新部署 │   │ 候选生成 → paired replay → 统计门禁    │──────┘
   │ held-out │   │ → holdout 确认 → 决策                 │  只改 prompt
   │ 验证     │   └────────────────────────────────────────┘  才回到 Phase 6
   └──────────┘
```

讲稿：
—— 循环的**入口**是生产 trace（Phase 4），**出口**是一个四选一的决策。
—— 只有决策是 `WINNER` 时才回到 Phase 6 重新部署；其余情况循环停在"没有可信的改进"。
—— **关键：整个循环里，只有 prompt 可以变。**模型、工具、代码、评估器、dataset 全部冻结。

## 6. 数据流：三份 dataset 如何守住可信度

```
dev_local (15) ──▶ Phase 2 本地基线        ┐
prod_sim  (20) ──▶ Phase 3 云上流量 ──▶ Phase 4 合并成样本池 ──▶ 稳定哈希切分
                                                                  ├─ control（调优用，反复看）
                                                                  └─ holdout（只在 Step 10 看一次）
verify    (20) ──────────────────────────────────────────────▶ Phase 6 最终验证（从不进池）
```

讲稿：
—— control 是允许反复迭代的；holdout 是"从未据以调优"的一批；verify 是"连 holdout 都算被污染了、
   留到最后的最干净一批"。三层防线，每一层挡一种自欺。
—— 切分用 `blake2b(scenario_id)`，是**纯函数** —— 重跑得到完全相同的切分。
   随机切分可以反复重摇到候选通过，而报告里看不出任何异常。

## 7. 本次运行的真实结局（剧透）

**5 个 prompt 候选 · 486 格 paired replay · 最终 `NO_CHANGE`。**

—— 一个候选（c2）在 control 上看起来赢了（质量 +0.0348，置信区间全正）
—— holdout 把它揭穿成**过拟合**（质量塌到 −0.0018，PolicyGrounding 回退 0.13）
—— 结论：没有可信的 prompt-only 改进；下一步需要换机制（超出本 SOP 范围）

讲稿：这不是失败的 demo。**能拒绝一个假的优化，才是自动优化敢接进生产的前提。**

---

# 第三部分 · CloudWatch Omni 的能力全景

## 8. Omni 的 16 个 MCP 工具 —— 全列表，高亮用到的

> Omni 不是独立进程，而是跑在 Kiro 扩展进程内的 TCP 服务，一次只服务一个 workspace。
> 本 demo 通过自建桥 `scripts/omni.mjs` 在纯 shell/CI 里调用它。

| # | 工具 | 用途 | 本 demo |
|---|---|---|---|
| 1 | **`check_credentials`** | AWS 凭证 doctor | ✅ 每个 phase 前置检查 |
| 2 | **`configure_omni`** | 项目配置 / 标记 onboarding 完成 | ✅ Phase 2 接线 |
| 3 | **`setup_skills`** | 把 skill 装到 coding agent | ✅ Phase 0（装本 skill 的容器） |
| 4 | **`manage_local_collector`** | 本地 OTLP collector 生命周期 | ✅ Phase 2/5 |
| 5 | **`local_server`** | 本地 dev server 生命周期 | ✅ Phase 2/5（每个变体重启） |
| 6 | **`manage_test_agent`** | agent 配置 + invoke + ping | ✅ 核心：Phase 2/5 全程 |
| 7 | **`search_local_telemetry`** | 本地 trace（list/get/delete，无 SQL） | ✅ 核心：找 trace、读 span 属性 |
| 8 | **`manage_datasets`** | dataset CRUD + 不可变版本 | ✅ 核心：Phase 2/4/5 |
| 9 | **`manage_evaluations`** | evaluator 列举/运行/自建 | ✅ 核心：Phase 2/5 打分 |
| 10 | `discover_agent_traces_metadata` | 云端 trace 存储的真实列名 | ⚠️ 探测阶段用过；流程中被 R8 阻塞 |
| 11 | `search_agent_traces` | 云端 trace 查询（DataFusion SQL） | ⚠️ 被 R8 阻塞 → 用 boto3 直读 CloudWatch 替代 |
| 12 | `manage_annotations` | trace 标注 | ⚠️ 被 R8 阻塞 → 失败模式落盘 `failure_modes.json` 替代 |
| 13 | `get_invocation_graph` | 云端调用图可视化 | ○ 未用（可视化增强项） |
| 14 | `get_context` | IDE 当前上下文 | ○ 未用（IDE 交互） |
| 15 | `notify_ui` | 通知 Omni Studio 刷新面板 | ○ 未用（IDE 交互） |
| 16 | `show_credentials_dialog` | 弹凭证配置框 | ○ 未用（IDE 交互） |

图例：✅ 深度使用 · ⚠️ 因环境限制(R8)改走等价通路 · ○ 未涉及

讲稿：
—— **9 个深度使用**，覆盖了 Omni 的核心价值：本地 telemetry、dataset 管理、评估。
—— 3 个云端工具（10-12）本可用，但本机环境有个 region/DNS 问题（R8）让 Omni 云端 endpoint
   连不上。我们用 AWS CLI/boto3 直读 CloudWatch Logs 替代 —— **结果完全一致**，
   而且这条通路不依赖 IDE，能进 CI。这本身是个有用的发现：Omni 的产物都落在标准 CloudWatch，
   没有 Omni 也能取。
—— 4 个未用的是 IDE 交互/可视化增强，与自动化循环无关。

## 9. Omni 提供的托管 Evaluator（实测 20+）

| 层级 | Evaluator |
|---|---|
| **TRACE**（单次调用） | Correctness · Faithfulness · Helpfulness · ResponseRelevance · Conciseness · Coherence · InstructionFollowing · Refusal · Harmfulness · Stereotyping · DeepEval.Bias/Toxicity |
| **TOOL_CALL**（工具使用） | ToolSelectionAccuracy · ToolParameterAccuracy · SkillSelectionAccuracy · SkillInstructionFollowing |
| **SESSION**（整段会话） | GoalSuccessRate · TrajectoryExactOrderMatch · TrajectoryInOrderMatch · TrajectoryAnyOrderMatch |
| **自定义** | LLM-as-judge（自定义 instructions + rating_scale + model_id） |

本 demo 用的 8 个：Correctness · InstructionFollowing · Helpfulness · Faithfulness ·
ToolSelectionAccuracy · ToolParameterAccuracy · GoalSuccessRate · **PolicyGrounding（自建）**

讲稿：
—— 为什么要自建 `PolicyGrounding`：内置的 `Correctness` 只看最终数字对不对，
   `InstructionFollowing` 只看有没有遵循 system prompt —— 但**v1 的 prompt 本身就没要求查政策**，
   所以它"遵循得完美"却答得错。PolicyGrounding 问一个正交问题：
   **回答里的政策数值，是从工具返回来的，还是模型编的？**
—— 一个招牌判例：agent 把 `create_return_label` 返回的"7 天内寄回"操作指引，
   当成退货政策数值报给用户。Correctness 看不见这个，PolicyGrounding 抓住了。

## 10. 声明式基础设施：整条评估管道进 CDK

```yaml
AWS::BedrockAgentCore::Runtime              # agent 本体
AWS::BedrockAgentCore::Evaluator            # 自定义 PolicyGrounding（云端）
AWS::BedrockAgentCore::OnlineEvaluationConfig  # 8 evaluator × 100% 采样 × 持续评估
AWS::Logs::DeliverySource/Destination/Delivery # trace 投递管道
```

讲稿：`tracingEnabled: true` 一行换来整条 trace 投递管道；改 prompt → asset hash 变 →
runtime 出新版本 → endpoint 自动跟上。**"自定义 evaluator + 云上持续评估"和 agent 一起
`cdk deploy`，不用手点控制台** —— 这对可追溯性是关键。

---

# 第四部分 · 循环里的详细步骤

## 11. 11 步 SOP 总览

> 这是 `omni-self-evolution` skill 的骨架。每一步都是"执行 → 验证 → 才能进下一步"。

```
Step 1  前置检查（4 项硬前提）
Step 2  冻结 baseline（manifest：prompt hash / model / 工具 / code SHA）
Step 3  只读拉取生产 trace
Step 4  冻结 evaluator 集（含每个 evaluator 的打分语义）
Step 5  给 baseline 打分 + 归纳失败模式
Step 6  构建合格样本集 + 稳定哈希切分 control/holdout
Step 7  生成 prompt-only 候选（每个只打一个失败模式）
Step 8  control 集上 paired replay（每变体 × 每样本 × N 遍）
Step 9  过统计门禁（6 道门）
Step 10 winner 在 holdout 上确认（抓过拟合）
Step 11 出决策 + 仅本地写回（WINNER / NO_CHANGE / NO_DECISION / ROLLED_BACK_LOCAL）
```

## 12. Step 1-4：把地基钉死

**Step 1 前置检查** —— 4 项，任一不满足就硬停：
—— 凭证可签名 · prompts.json 里的 model 真生效（不是装饰）· `OMNI_PROMPTS_OVERRIDE` 可用 ·
   span 上有 `llm.prompt_template.version`（否则 replay 无法归因变体）

**Step 2 冻结 baseline** —— 写 manifest：prompt 版本 + **跨进程稳定的 hash**（blake2b，
不用内置 `hash()`，因为它受 `PYTHONHASHSEED` 随机化）+ model + 工具列表 + code SHA。
讲稿：Step 11 写回 winner 前要拿这个 hash 校验文件没被中途改过。

**Step 3 只读拉取** —— 生产 trace 拉到本地。**只读是硬约束**：不写、不改、不删任何生产资源。

**Step 4 冻结 evaluator** —— 选定评估器，**在第一次 replay 之前**锁死每个的打分语义
（方向/值域/及格线/归一化公式）。
讲稿：实测在这里抓到 `Helpfulness` 真实值域是 [0,6] 不是 [0,1] ——
如果不核对，归一化会把所有 ≥1 的分钳到 1.0，整个维度失去分辨率。

## 13. Step 5-6：找到病根，切好数据

**Step 5 打分 + 归纳失败模式** —— 用冻结的 evaluator 给 baseline 打分，把低分 trace
按维度归类。本次结果：

| 失败模式 | 影响 | 三处一致 |
|---|---|---|
| `low_Helpfulness` | **31/31** | 本地 0%、云上 0% 及格率 |
| `low_Correctness` | 8 | |
| `low_GoalSuccessRate` | 7 | |

讲稿：`Helpfulness` 全量命中，三条独立观测（本地基线、云上评估、导出分类）给出一致信号 ——
Phase 5 的主目标没有争议。

**Step 6 构建样本集 + 切分** —— 门槛：eligible ≥12 · control ≥5 · holdout ≥5。
—— 实测第一次撞门：只用 prod_sim 时 holdout 只有 3 条 < 5 → **`NO_DECISION`**。
—— 正确应对：**不许降门槛、不许重切、不许合成样本**，只能补真实流量。
   而对同一 dataset 再发流量不改变切分（scenario_id 集合没变）——
   必须扩大 scenario 池本身。补 dev_local 到云上后：control 25 / holdout 6，过门。

## 14. Step 7：生成候选 —— 每个只改一件事

| 候选 | 打哪个失败模式 | 机制 |
|---|---|---|
| `c1-explicit-constraints` | 自相矛盾 + 不给替代方案 | 只**追加**约束 |
| `c2-restructured-procedure` | 同 c1 | **重构**成四步作业流程 |
| `c3-arithmetic-rules` | 优惠券分摊算错 | 只加算术规则 |

讲稿：
—— 每个候选是 baseline 的完整拷贝，**只改 system prompt 内容**，model 及其余键 byte-identical。
   机器校验过"除 prompt 外全同"。
—— c1 和 c2 打**同一个**失败模式，一个"加约束"一个"改结构" ——
   故意的，用来比较哪种手段更有效。
—— 候选里**不许出现任何订单号或期望金额** —— 那是把答案背进 prompt，不会泛化。

## 15. Step 8：paired replay —— 为什么必须是脚本

```
4 变体 × 25 样本 × 3 遍 = 300 格
```

三条硬约束（都在代码里落实，不靠自觉）：
—— **baseline 必须现场重跑**，不复用历史分数（不同流量/时刻不构成配对）
—— **变体只靠 `OMNI_PROMPTS_OVERRIDE` 切换**，每次重启 dev server（环境变量是进程级的）
—— **每次调用核对 trace 上的 prompt 版本**，不符即丢弃该格

讲稿：
—— 为什么不让 agent 自己发这些调用？因为这是确定性记账，手工驱动会漏格、重格，
   而漏掉的格子不报错，只让某个 arm 的均值悄悄偏移。
—— **版本核对这条约束在真跑时救了场**：一度整整一个 arm（56 格）被标成 `v1-draft` ——
   `OMNI_PROMPTS_OVERRIDE` 生效了、内容变了，但版本标签错了，没有这条核对，
   报告会漂亮、结果却无法归因。

## 16. Step 9：六道统计门

| 门 | 阈值 | 论证 |
|---|---|---|
| 均值质量提升 | ≥ **+0.03** | 低于此值落在判官自身 run-to-run 波动内（实测约 0.02） |
| paired bootstrap CI | 95%，**下界 ≥ 0** | 对**样本**重采样，问"能否泛化到新输入"，不是"这批测得多准" |
| 逐 evaluator 回退 | 每个 ≤ **0.02** | 拦"靠牺牲一维换平均分" |
| 延迟回退 | ≤ **10%** | 靠更长推理赢的 prompt，代价不在质量分里 |
| token 回退 | ≤ **10%** | 同上，成本维度 |
| safety | **零新增失败** | 硬门，不可交易 |

讲稿：
—— 这一步也**必须是脚本**：delta / bootstrap / 逐维判定都有唯一正确答案，
   多 arm 分数表上的 LLM 算术不可靠。
—— bootstrap 对**样本**重采样，不对调用重采样 —— 这是"泛化到新用户"和"测得准"的区别。

## 17. Step 9 结果：c2 的"近失"

```
候选   Δ质量      95% CI              未通过的门
c1    +0.0220   [-0.001, +0.046]    质量<0.03；CI 下界<0
c2    +0.0348 ✅ [+0.015, +0.056] ✅  仅延迟 11.11% > 10%
c3    +0.0088   [-0.010, +0.030]    质量太小；CI<0；延迟；token
→ NO_CHANGE
```

讲稿：
—— c2 质量门和置信区间都过了，**只差延迟 1.11 个百分点**。
—— 诱惑：把延迟门从 10% 调到 12% 就能让它过。**纪律：不能这么做。**
   一个为了放行眼前候选而选的门槛不是门槛。所以改的是候选（出了压缩版 c4），
   而 c4 把质量提升完全压没了（+0.0348 → −0.0013）—— 证明质量与延迟在改措辞这条路上分不开。

## 18. Step 10：holdout 揭穿过拟合 —— 全场最关键一页

```
c2 在 control：Δ +0.0348   CI [+0.015, +0.056]   ← 看起来是真提升
c2 在 holdout：Δ -0.0018   CI [-0.054, +0.058]   ← 提升消失
              PolicyGrounding 回退 -0.1296        ← control 上没暴露的维度
```

讲稿：
—— 这是整个 demo 的**决定性时刻**。换 6 条从没调过的样本，c2 的提升塌成 0，
   还在一个之前看不见的维度上掉了 0.13。
—— **如果当初放宽延迟门让 c2 通过、直接上线，就会部署一个实际更差的 prompt，
   而 control 上的报告会一路绿灯。** 是 holdout、不是任何阈值调整，挡住了它。
—— 这就是 Step 10 存在的**全部理由**：抓"在你盯着的数据上调到通过"这个最常见的自欺。

## 19. Step 11：四种决策，必须区分

| 决策 | 含义 | 本次 |
|---|---|---|
| `WINNER` | 通过全部门禁 + holdout 确认 | — |
| **`NO_CHANGE`** | 测了，但没人赢。**baseline 站得住** | ✅ 本次结局 |
| `NO_DECISION` | 根本没测成（样本不足/无 trace） | Phase 4 一度触发 |
| `ROLLED_BACK_LOCAL` | 本地写回后又回退 | — |

讲稿：
—— **`NO_CHANGE` 和 `NO_DECISION` 必须区分** —— 前者是"测了没赢"，后者是"没测成"。
   混同会让一次没测成的运行被读成"基线已验证"。
—— 本次 `NO_CHANGE` 是**成功**的运行：它以可复现、可审计的证据确立了
   "这些 prompt-only 假设都打不过 baseline"，并指明下一步（换机制，不是换措辞）。

---

# 第五部分 · Human in the Loop vs Human on the Loop

## 20. 两种模式的定义

| | **Human IN the Loop** | **Human ON the Loop** |
|---|---|---|
| 人的位置 | 在**每一步**里，系统等人点头才继续 | 在**循环之外**，系统自动跑，人监督结果 |
| 类比 | 副驾驶手把手 | 值班工程师看仪表盘 |
| 优点 | 每个决定都有人背书 | 可扩展、快、不被人拖慢 |
| 代价 | 慢、不可扩展、人疲劳 | 系统可能自动做错事 |

讲稿：这不是二选一。**成熟的自动化是在不同环节选不同模式。**

## 21. 本 demo 的实际划分

```
┌─────────────────────────────────────────────────────────┐
│  HUMAN ON THE LOOP（自动跑，人看结果）                    │
│                                                          │
│   Step 3 拉 trace ─▶ Step 5 归纳失败 ─▶ Step 7 生成候选   │
│   ─▶ Step 8 replay ─▶ Step 9 门禁 ─▶ Step 10 holdout     │
│                                                          │
│   全程无人干预，产出可审计的证据和一个建议                │
└────────────────────────┬─────────────────────────────────┘
                         │  决策点
┌────────────────────────▼─────────────────────────────────┐
│  HUMAN IN THE LOOP（必须人点头）                          │
│                                                          │
│   • 是否上线 WINNER（Invariant 3：SOP 绝不自己部署）      │
│   • 是否人工豁免某道门（--waive-gate，且必须给理由）      │
└──────────────────────────────────────────────────────────┘
```

讲稿：
—— 循环的**分析与评估**部分是 human-on-the-loop：系统自动从 trace 跑到一个带证据的建议，
   人不需要盯着每一步。
—— 循环的**出口**（上线、豁免）是 human-in-the-loop：**这些决定有生产爆炸半径，SOP 无法评估。**

## 22. 三个机制把边界落到实处

**① 只读 + 不部署（Invariant 1 & 3）**
—— SOP 全程不写任何生产资源、绝不 `cdk deploy` / 发布 prompt / 改 canary。
—— 它产出的是**建议**，不是行动。上线是人在循环里的动作。

**② 可审计的人工豁免（`--waive-gate`）**
—— 用户说"c2 可以"时，我们没有偷偷调阈值。而是：**阈值一字不改、门仍记 ❌**，
   豁免连同**理由**写进 JSON，结局另记 `WINNER_BY_HUMAN_WAIVER`。
—— 事后任何人都能看到：这个候选没过哪道门、是谁、以什么理由放行的。
—— 这一页的真实教训：**豁免只作用于人主动承担的那道门（延迟），不触碰质量与安全门。**
   c2 被豁免延迟后，质量门在 holdout 上照样没过 —— 豁免机制没有把它洗成 winner。

**③ holdout 是自动的、不可豁免的**
—— 过拟合检测**不交给人判断** —— 人会倾向于相信自己想要的候选。
—— holdout 由稳定哈希在候选存在之前就切好，人无法事后重切。它是循环里
   最不该有人插手的一环，所以把它做成完全自动、完全确定。

## 23. 为什么这个划分是对的

—— **人擅长的**：判断"6s→6.6s 换质量提升值不值"这种业务权衡（豁免延迟门）。
—— **人不擅长的**：在一堆分数表里判断"这个提升是真的还是过拟合" ——
   人会被"我调了半天的候选"这个沉没成本带偏。这件事交给 holdout + 冻结的统计门。
—— **绝不能交给人的**：偷偷调阈值让心仪的候选通过。所以阈值冻结、豁免留痕。

讲稿：
—— 一句话：**让人做价值判断，让机器做防自欺。**
—— 本次运行是这个原则的最好例证：人（合理地）想要 c2，机器（正确地）用 holdout 说不。
   两者都对自己那部分负责 —— 最后没有上线一个假的优化。

## 24. 收尾

**这个 demo 真正证明的：**

—— 不是"AI 能自动优化 prompt"（那太容易，也不可信）
—— 而是"**一个能拒绝假优化的自动优化循环**长什么样"：
   - 只读、不部署、只改 prompt
   - 每个数字可复现、每道门有论证、每次豁免留痕
   - holdout 自动挡过拟合，人只做价值判断

—— **能拒绝，才敢自动。** 这是把 self-evolution 接进生产的前提。

配套：完整过程见 [BUILD-LOG.md]（含 20+ 个实测踩坑，其中约一半是"不报错的静默失败"）·
调用流程见 [RUNBOOK.md] · 可复用方法论见 [omni-self-evolution SKILL.md]。
