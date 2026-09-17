# AgentCore Self-Evolution Demo — 实施计划

> 场景：电商订单售后客服 · 部署：CDK + AgentCore Direct Code Deploy (S3 zip) · Region: **us-east-1** · Profile: **default**

---

## Part 1 — MCP / Skill 验证结果（已完成，全部实测）

### Omni MCP：可用，但**绑定单一 workspace**

Omni 的 MCP 不是独立进程 —— `mcp-proxy.js` 从 `<workspace>/.omni/mcp-port`
读端口，然后 TCP 连到**运行中的 Kiro 扩展进程**。实测：

```
$ cat .../agents/langgraph/.omni/mcp-port     → 52618
$ lsof -i :52618                              → Kiro Helper (PID 98719)  LISTEN
$ workspace/verify  {workspace: <langgraph>}   → OK
$ workspace/verify  {workspace: <本项目>}       → ERROR -32001
    "Workspace mismatch: extension serves ".../agents/langgraph"
     but client requested ".../self-evolve-demo-with-omni""
```

**含义（阻塞项）**：本项目要用 Omni MCP，必须让 Kiro 打开本项目目录。
详见 Part 4 风险 R1。

### 16 个工具全部枚举 + 关键工具实测调用

| 工具 | 状态 | 备注 |
|---|---|---|
| `check_credentials` | ✅ 实测 | `can_sign:true, ready:true`, account `613477150601`, default-chain |
| `manage_evaluations` | ✅ 实测 | `list_evaluators` 返回 20+ 个（见下） |
| `discover_agent_traces_metadata` | ✅ 实测 | 返回 `totalKeys: 0` —— 云端当前窗口无 trace（符合预期，还没部署） |
| `search_agent_traces` | ✅ schema | DataFusion SQL；必须 `"@telemetry_type"='traces'` + `BETWEEN to_timestamp(秒) AND to_timestamp(秒)` 两端都绑定 |
| `search_local_telemetry` | ✅ schema | list/get/delete，window 用**毫秒**（和云端的秒相反，易踩坑） |
| `manage_datasets` | ✅ schema | `dataSource: local\|cloud`；`examples[]` 支持 `scenario_id / turns{input,expected_response} / expected_trajectory / assertions / metadata`；`create_version` 可冻结不可变版本 |
| `manage_test_agent` | ✅ schema | `set_start_command / set_agent_schema / invoke_agent / ping`；内置 preset **AgentCore** = `/invocations` + `{"prompt":"…","sessionId":"…"}` |
| `manage_annotations` | ✅ schema | 标注 trace，用于筛"差"trace |
| `local_server` / `manage_local_collector` | ✅ schema | 本地 OTLP collector + dev server 生命周期 |
| `configure_omni` | ✅ schema | `prompt_setup_complete` 注册 prompts.json 路径 |
| `get_context`/`notify_ui`/`show_credentials_dialog`/`get_invocation_graph`/`setup_skills` | ✅ schema | |

### 可用 Evaluator（实测拉取，全部 AgentCore 托管）

- **TRACE**：Correctness、Faithfulness、Helpfulness、ResponseRelevance、Conciseness、
  Coherence、InstructionFollowing、Refusal、Harmfulness、Stereotyping、
  ThirdParty.DeepEval.Bias / .Toxicity
- **TOOL_CALL**：ToolSelectionAccuracy、ToolParameterAccuracy、
  SkillSelectionAccuracy、SkillInstructionFollowing
- **SESSION**：GoalSuccessRate、TrajectoryExactOrderMatch、
  TrajectoryInOrderMatch、TrajectoryAnyOrderMatch
- 可自建：`create_evaluator`（LLM-as-judge，自定义 instructions + rating_scale + model_id）

### Skill：21 个，关键 3 个已通读

- **`omni-self-evolution`**（283 行）—— 核心。11 步程序化脚本，硬性 gate：
  `min_eligible_examples 12` / `holdout_fraction 0.30` / `min_quality_delta 0.03`
  / bootstrap CI 下界 ≥ 0 / safety 硬门 / 只允许改 prompt 且**禁止**动生产资源。
  产物落 `.omni/self-evolution/<run_id>/`。
  → **本 Demo 第 4~5 步直接跑这个 skill**。
- **`omni-prompt-setup`**（480 行）—— `prompts.json` + `prompt_loader.py` +
  `OmniPromptProcessor`。这是 variant-aware replay 的前提（self-evolution 要求
  能用显式 version/override 隔离变体），必须在 Phase 1 就做。
- **`omni-test-agent-invocation`**（814 行）—— AgentCore preset 契约、
  start command（`agentcore dev -l --skip-deploy`）、trace 验证闭环。

### AWS 侧就绪度（实测）

| 项 | 结果 |
|---|---|
| `sts get-caller-identity` | `arn:aws:iam::613477150601:user/yagrxu`（IAM user，非 role） |
| profile default region | `ap-southeast-1` → **所有代码显式写 us-east-1，不吃 profile 默认值** |
| `xray get-trace-segment-destination` (us-east-1) | `CloudWatchLogs` / **ACTIVE** —— 生产可观测性前置条件已满足 |
| X-Ray indexing rule | Default probabilistic **100%** 采样 |
| CFN 资源类型 | `AWS::BedrockAgentCore::{Runtime, RuntimeEndpoint, Dataset, Evaluator, OnlineEvaluationConfig}` 全部存在 |
| `aws-cdk-lib` 2.269.0 | 含 `aws-bedrockagentcore`：L1 `CfnRuntime/CfnRuntimeEndpoint/CfnEvaluator/CfnOnlineEvaluationConfig` + L2 `evaluation/`（`OnlineEvaluationConfig`、`EvaluatorSelector.builtin()`、`BuiltinEvaluator`） |
| Direct Code Deploy | `AgentRuntimeArtifact.CodeConfiguration` = `{Code:{S3:{Bucket,Prefix}}, Runtime: PYTHON_3_10..3_14\|NODE_22, EntryPoint: string[1..2]}` → **无需 Docker** |
| EntryPoint 支持 ADOT | 内部服务已验证用法：`["opentelemetry-instrument", "agent.py"]`（2 元素上限刚好） |
| 已有 AgentCore runtime | `cat_demo_strands`、`cat_demo_langgraph`（us-east-1，别人的 demo）→ **不碰** |
| Bedrock 模型 | Haiku 4.5 / Sonnet 4.5 / Sonnet 5 / Opus 5 均可见 → 满足"换模型"这一优化轴 |

---

## Part 2 — 场景设计

### Agent：`order_support_agent`（Strands + BedrockAgentCoreApp，Python 3.12）

工具后端是**打包进 zip 的确定性 JSON fixture**（不打外部网络）——
这同时满足 self-evolution skill 的 "replay isolation 必须可证明" 要求。

> ⚠️ **工具分两层，这是施工中修正过的重要设计**（详见 BUILD-LOG Phase 1.15）。
> 第一版工具做得太强（`check_return_eligibility` 直接返回完整裁决、
> `calculate_refund` 包办全部算术），结果实测发现 **v1 把所有场景都答对了** ——
> Demo 前提"一开始效果不好"直接不成立。
> 于是让 BASIC 层退回真实后端该有的样子，把计算类工具移到 ENHANCED 层，
> 成为 Phase 5「加 tool」这条优化轴的**实际内容**。

**BASIC 层**（默认，baseline / c1 / c3 用；`AGENT_TOOL_TIER=basic`）：

| 工具 | 返回什么 | 刻意不做什么 |
|---|---|---|
| `get_order(order_id)` | `order_date` + `today` + 商品原始字段 + 运费 + 优惠券 | **不算"距今多少天"** → 模型必须自己做跨月日期减法 |
| `get_refund_policy(category)` | 原始政策字段 + 三条横切规则**原文** | **不给结论** → 是否超窗口、收多少费、运费退不退都要模型自己套 |
| `search_knowledge_base(query)` | **干扰项**：企业帮助中心式 FAQ，措辞友好但无可执行数值（"一般来说 7-15 天"） | 不说谎、只是不精确不分类目 —— 真实 FAQ 就这样，所以这个干扰是公平的 |
| `create_return_label(order_id, skus)` | RMA 单号（写内存，无外部副作用） | 内部有资格校验，不可退时报错 |

**ENHANCED 层**（Phase 5 candidate c2；`AGENT_TOOL_TIER=enhanced`）额外提供：

| 工具 | 把什么从模型手里拿走 |
|---|---|
| `check_return_eligibility(order_id, reason)` | 日期减法 + 套政策 + 判边界 |
| `calculate_refund(order_id, items)` | 券按比例分摊 → 按件拆分 → 折旧费 → 运费规则 |
| `get_coupon_allocation(order_id)` | 单独暴露券分摊明细 |

### v1 故意做差（Demo 的起点）

- system prompt 只有一句 "You are a helpful customer service assistant. Be polite."
- 不要求先查工具、不要求引用真实政策、不给算术规则
- 模型选 **Haiku 4.5**（更容易走捷径）

**实测失败分布**（BASIC 层工具 + v1 prompt，真实 Bedrock 调用）：

| 场景 | oracle | v1 实际 | 失败类型 |
|---|---|---|---|
| `dev_012` 券80+多品+部分退 | **¥44.88** | **¥73.33** | 用户只要退 1 件，它按 2 件算；券分摊也算错 |
| `dev_008` home 28 天 | 超 21 天窗口 → **不可退** | 「**✅ 可以退货**…折旧费约 ¥23」 | **自相矛盾 + 对不可退商品报金额** → 触发确定性硬门 |
| `dev_013` 破损超窗口 | ¥1605 | 结论对但**没给金额** | 不完整 |
| `dev_002` 超窗口换新 | 换新 | 正确 | — |
| `dev_011` 券200 单品 | ¥989.10 | 正确 | — |

失败率约 40–60%，正好落在 R7 目标区间："明显有问题但仍是个能用的客服"。
`dev_008` 是演示招牌案例 —— 真实业务里这就是一条客诉。

### 3 个 Dataset（对应用户要求的不同阶段用不同数据）

| Dataset | 阶段 | 规模 | 用途 |
|---|---|---|---|
| `ds_dev_local` | Phase 2（本地） | 15 | 本地基线，暴露问题 |
| `ds_prod_sim` | Phase 3（云上） | 20 | 模拟真实用户流量，边界 case 更多 |
| `ds_verify` | Phase 6（云上） | 20 | **完全 held-out**，同分布不同订单 → 证明提升不是记住了答案 |

每条 example 都带 `expected_response` + `expected_trajectory` + `assertions`。

### Evaluator 组合（8 个内置 + 1 个自建）

| 层级 | Evaluator | 抓什么 |
|---|---|---|
| TRACE | `Builtin.Correctness` | 金额/日期/政策事实对不对 |
| TRACE | `Builtin.InstructionFollowing` | 有没有按 system prompt 的流程走 |
| TRACE | `Builtin.Helpfulness` | 有没有给替代方案 |
| TRACE | `Builtin.Faithfulness` | 回答是否被工具返回的 context 支撑（工具输出作 `retrievedContexts`） |
| TOOL_CALL | `Builtin.ToolSelectionAccuracy` | 选对工具没有 |
| TOOL_CALL | `Builtin.ToolParameterAccuracy` | 参数抽对没有 |
| SESSION | `Builtin.TrajectoryInOrderMatch` | 工具顺序 |
| SESSION | `Builtin.GoalSuccessRate` | 用户目标是否达成 |
| TRACE（自建） | **`PolicyGrounding`** | **是否引用了 `get_refund_policy` 真实返回的政策，还是编的** ← 直击 v1 病根 |

安全硬门（self-evolution skill 要求）：`Builtin.Harmfulness` + `Builtin.Stereotyping`。

---

## Part 3 — 6 个 Phase（对齐用户的 6 步）

### Phase 0 — 项目骨架 + Omni 接线

- `git init`；建下面的目录树
- 写 `.mcp.json`：把 Omni MCP server 指向**本 workspace**
- `scripts/omni.mjs`：我已实测通的 JSON-RPC 桥（`workspace/verify` → `initialize`
  → `tools/call`），让 traffic / export 脚本能在 CI 或纯 shell 里调 Omni 工具，
  不依赖 IDE 里的 agent 会话
- 通过 `setup_skills` 把 21 个 Omni skill 装进 `.kiro/skills/`
- **需要你操作**：在 Kiro 里打开本项目目录（见 R1）

### Phase 1 — v1 agent + 本地埋点（对应需求 1）

- `agent/agent.py`：Strands + `BedrockAgentCoreApp`，`/invocations`，5 工具
- `agent/fixtures/{orders,policies}.json`
- `agent/prompts.json` + `agent/prompt_loader.py` + 注册 `OmniPromptProcessor`
  （走 `omni-prompt-setup` skill）→ v1 存为 `order_support-v1`
- ADOT 埋点（`aws-opentelemetry-distro>=0.18.0`）
- `manage_test_agent(set_start_command)` + `set_agent_schema(preset="AgentCore")`
- **验收**：`search_local_telemetry(list)` 有非 error trace，且
  `llm.prompt_template.version == "order_support-v1"`

### Phase 2 — 本地 dataset + 多 evaluator 基线（对应需求 2）

- `manage_datasets(create, dataSource=local, name=ds_dev_local, examples=[…15])`
  → `create_version` 冻结 v1
- `manage_evaluations(create_evaluator)` 建 `PolicyGrounding`
- 15 条全部 `invoke_agent` → 收 trace ID
- 9 个 evaluator 各跑一遍（`traceIds` + `datasetId` 提供 ground truth）
- **产出** `reports/01-local-baseline.md`：预期 Correctness / PolicyGrounding
  明显偏低 → **这就是"一开始效果不好"的证据**

### Phase 3 — CDK 部署 + 云上流量测试（对应需求 3）

`cdk/` (TypeScript)：

- **`AgentStack`**
  - `s3_assets.Asset`：bundling 把 `agent/` + `pip install --target`（vendored
    deps，target platform 对齐 PYTHON_3_12）打成 zip
  - IAM exec role：`bedrock:InvokeModel`（Haiku/Sonnet + inference profile）、
    `logs:*` on `/aws/bedrock-agentcore/runtimes/*`、`cloudwatch:Ingest`、
    `cloudwatch:CallWithBearerToken`、`xray:PutTraceSegments`
  - `CfnRuntime`：`codeConfiguration{ code:{s3}, runtime:'PYTHON_3_12',
    entryPoint:['opentelemetry-instrument','agent.py'] }`，
    `networkMode: PUBLIC`，env `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` /
    `AGENT_OBSERVABILITY_ENABLED=true` / `MODEL_ID` / `PROMPT_VERSION`
  - `CfnRuntimeEndpoint` → `v1`
- **`EvalStack`**
  - `CfnEvaluator`：`PolicyGrounding`（和本地那个同 rubric，保证可比）
  - `OnlineEvaluationConfig`：对该 runtime 的 trace 持续跑 8 个 builtin +
    PolicyGrounding → 云上 evaluator 分数自动产生
- `scripts/traffic.py`：读 `datasets/prod_sim.json`，每条一个独立 `sessionId`，
  错峰 `InvokeAgentRuntime` → 模拟真实用户
- 等 trace 落地（X-Ray→CWL 已 ACTIVE，通常 ~5-10 分钟），
  `search_agent_traces` SQL 拉结果 + `manage_evaluations(results, dataSource=cloud)`
- **产出** `reports/02-cloud-baseline.md`：云上也不够好，且能点到具体 case

### Phase 4 — 从云上导出差 trace 到本地（对应需求 4）

- `scripts/export_bad_traces.py`：
  1. `discover_agent_traces_metadata` 拿真实列名
  2. SQL 按「evaluator 分数低 / status=error / PolicyGrounding fail」筛
  3. `SELECT "@record"` 拉全量 span
  4. 脱敏 → 写 `.omni/self-evolution/<run_id>/source-traces.jsonl`
- 按 self-evolution skill 建 oracle、切
  `development / holdout(30%) / control` 三份并 `create_version` 冻结

### Phase 5 — 3 条优化轴 + 本地 A/B（对应需求 5）

三个 candidate，正好覆盖用户说的三种手段：

| Candidate | 手段 | 内容 |
|---|---|---|
| `c1_prompt_v2` | **改 prompt** | 显式工具调用协议 + 必须引用 `get_refund_policy` 原文 + 算术规则 |
| `c2_tool_tier_enhanced` | **加 tool** | `AGENT_TOOL_TIER=enhanced` → 追加 `check_return_eligibility` / `calculate_refund` / `get_coupon_allocation`，把日期减法与券分摊算术交给代码 |
| `c3_model_swap` | **换模型** | v2 prompt 跑 **Sonnet 4.5**（替代 Haiku 4.5） |

- 变体隔离：`OMNI_PROMPTS_OVERRIDE`（prompt/model）+ `AGENT_TOOL_TIER`（tools），
  loader 与 agent 均已支持 → paired replay，每 scenario × variant 跑 3 次
- ⚠️ c2 改的是工具集，严格来说超出了 self-evolution skill「仅改 prompt」的边界。
  报告里会**明确标注它是 tool-change candidate**，与 c1/c3 分开呈现 ——
  不混进"prompt-only 门禁"的结论里。
- 评估 c2 时期望轨迹换用 `metadata.expected_trajectory_enhanced`
  （BUILD-LOG Phase 1.16：轨迹必须按工具层分开，否则 baseline 分数虚低、提升被夸大）
- 同样 9 个 evaluator，只在 holdout + control 上算 gate：
  `min_quality_delta ≥ 0.03`、bootstrap CI 下界 ≥ 0、
  单 evaluator 回退 ≤ 0.02、p95 延迟 / token 回退 ≤ 10%、safety 硬门全过
- **产出** `reports/03-local-ab.md` + `decision.json`；winner 原子写回
  `prompts.json`（带 `apply-state.json` 可回滚）

### Phase 6 — 重新部署 + 第三份 dataset 云上验证（对应需求 6）

- `cdk deploy`：asset hash 变 → runtime 新版本 → endpoint `v2`
- `scripts/traffic.py --dataset datasets/verify.json`（**全新 held-out 20 条**）
- 读 online-eval 结果 → **v1 vs v2 并排对比表**
- **产出** `reports/04-cloud-verify.md`：每个 evaluator 的 before/after

---

## Part 4 — 目录树

```
self-evolve-demo-with-omni/
├── .mcp.json                     # Omni MCP → 本 workspace
├── .kiro/skills/                 # setup_skills 装进来的 21 个 skill
├── agent/
│   ├── agent.py                  # Strands + BedrockAgentCoreApp, 工具分层开关
│   ├── tools.py                  # BASIC 4 个 + ENHANCED 3 个
│   ├── prompt_loader.py          # Omni 托管 prompt + OmniPromptProcessor
│   ├── prompts.json              # v1 / v2 / v3 版本历史
│   ├── fixtures/{orders,policies,faq}.json
│   └── requirements.txt
├── datasets/
│   ├── dev_local.json            # 15  Phase 2
│   ├── prod_sim.json             # 20  Phase 3
│   └── verify.json               # 20  Phase 6  (held-out)
├── evaluators/
│   ├── policy_grounding.json     # 自定义 evaluator rubric（本地+云端共用）
│   └── semantics.json            # 打分语义与门禁参数的冻结声明
├── cdk/
│   ├── bin/app.ts
│   └── lib/{agent-stack,eval-stack}.ts
├── scripts/
│   ├── omni.mjs                  # MCP JSON-RPC 桥（已实测）
│   ├── omni_client.py            # Python 侧封装（转调 omni.mjs）
│   ├── build_datasets.py         # 由 tools.py 纯函数渲染 oracle
│   ├── build_agent_bundle.sh     # Direct Code Deploy 打包
│   ├── local_baseline.py         # Phase 2
│   ├── traffic.py                # Phase 3/6 云上流量模拟
│   ├── export_bad_traces.py      # Phase 4
│   └── ab_replay.py              # Phase 5 paired replay
├── reports/01..04-*.md
└── docs/{PLAN.md,DEMO-SCRIPT.md}
```

---

## Part 5 — 风险

| # | 风险 | 影响 | 处置 |
|---|---|---|---|
| **R1** | **Omni MCP 绑定单一 workspace**（已实测报错） | 阻塞所有 MCP 步骤 | 需要你在 Kiro 里打开本项目目录。在那之前我可以先把代码 / CDK / dataset 全部写完（不需要 MCP），MCP 相关步骤留到最后一起跑 |
| **R2** | Direct Code Deploy 的依赖打包（架构 / 平台轮子） | 部署失败 | Phase 3 开头先部一个**最小 runtime** 实测（~10 分钟）。跑不通自动回退 ECR 容器（Dockerfile 我一并准备好） |
| **R3** | 云端 trace / eval 有 5-10 分钟延迟 | Demo 节奏卡顿 | 脚本内置轮询等待 + 超时提示；Demo 讲稿里把这段安排成"讲解 evaluator 原理"的时间 |
| **R4** | profile 默认 region 是 ap-southeast-1，AgentCore 在 us-east-1 | 静默连错 region，查不到东西 | 所有代码 / CDK / 脚本**显式** `us-east-1`，绝不依赖 profile 默认 |
| **R5** | 账号里已有 `cat_demo_strands` / `cat_demo_langgraph` runtime | 误删/误改别人的 demo | 全部资源加 `selfevolve-demo` 前缀 + 独立 CDK stack；只 create，不 touch 已有资源 |
| **R6** | 云上 evaluator 成本 | 9 evaluator × 20 例 × 2 轮 ≈ 360+ 次 judge 调用 | 用 Haiku 做 judge model；可用 `--limit` 缩小演示规模 |
| **R7** | v1 "要够差但不能崩" | Demo 讲不出提升 | ✅ **已闭环**：第一版工具太强导致 v1 全答对，已重设计为 BASIC/ENHANCED 两层；实测失败率 40–60%，落在目标区间（BUILD-LOG Phase 1.15） |
