# 构建日志 (Build Log)

> **这是一份 append-only 的施工日志。** 每一步都记录：**执行了什么命令 → 原始输出 → 由此得出的结论/决策**。
> 目的是让任何人（包括未来的你）能完整追溯"为什么这样做"，而不只是看到最终结果。
>
> 相关文档：[PLAN.md](PLAN.md)（设计与计划）· [DEMO-SCRIPT.md](DEMO-SCRIPT.md)（演示讲稿）
>
> 环境：macOS Darwin 25.6.0 · AWS profile `default` · account `613477150601` · **region us-east-1**（显式，不吃 profile 默认值 ap-southeast-1）

---

## 目录

- [Phase -1 — MCP / Skill / AWS 能力验证](#phase--1)
- [Phase 0 — 项目骨架与 Omni 接线](#phase-0)
- [Phase 1 — v1 agent（故意做差）+ 本地埋点](#phase-1)
- [Phase 2 — 本地 dataset + 多 evaluator 基线](#phase-2)
- [Phase 3 — CDK 部署 + 云上流量测试](#phase-3)
- [Phase 4 — 从云上导出差 trace](#phase-4)
- [Phase 5 — 三条优化轴 + 本地 A/B](#phase-5)
- [Phase 6 — 重新部署 + held-out 云上验证](#phase-6)

---

<a name="phase--1"></a>
## Phase -1 — MCP / Skill / AWS 能力验证

**目标**：在写一行业务代码之前，先用实测（不是读文档、不是推测）确认三件事能不能做到：
(a) Omni MCP 工具能不能调；(b) Skill 里的流程是否可执行；(c) AWS 侧 AgentCore + 可观测性 + CDK 支持是否到位。

### -1.1 发现：Omni MCP 不在当前 Claude Code 会话里

用户给的 MCP 配置指向：

```json
{"mcpServers": {"cw-omni-agents-extension": {
  "command": "/Users/yagrxu/.nvm/versions/node/v24.14.0/bin/node",
  "args": ["/Users/yagrxu/.kiro/extensions/amazon-internal.omni-studios-1.0.5151/out/mcp-server/mcp-proxy.js",
           "--workspace", "/Users/yagrxu/me/collaborations/2026/aiops/serverless-demo-for-aiops/agents/langgraph"]}}}
```

当前会话已加载的 MCP 只有 builder-mcp / holmes / pencil / sentral / outlook，**没有 Omni**。
所以不能直接调工具，需要先搞清楚这个 MCP server 到底是什么形态。

### -1.2 读 mcp-proxy.js 源码 → 关键发现：它只是个 TCP 隧道

```bash
$ ls -la /Users/yagrxu/.kiro/extensions/amazon-internal.omni-studios-1.0.5151/out/mcp-server/
mcp-proxy.js   mcp-proxy.ps1   mcp-proxy.sh
```

读 `mcp-proxy.js`（129 行）后确认它的行为：

| 代码位置 | 行为 |
|---|---|
| `resolvePort()` | 从 `<workspace>/.omni/mcp-port` 读一个端口号 |
| `connect()` | `net.createConnection({port, host:'127.0.0.1'})` —— 连本机 TCP |
| 握手 | 先发 `{"method":"workspace/verify","params":{"workspace":<path>}}`，验过才开隧道 |
| `startTunnel()` | 之后 stdin ⇄ socket 逐行透传（就是裸 MCP JSON-RPC） |

**结论**：Omni MCP server **不是独立进程**，而是跑在 Kiro 扩展进程内的一个 TCP 服务。
proxy 只是 stdio→TCP 的适配器。

### -1.3 验证：server 确实在跑，且绑定单一 workspace

```bash
$ cat .../agents/langgraph/.omni/mcp-port
52618

$ lsof -nP -iTCP -sTCP:LISTEN | grep -i node
Kiro\x20H 98719 yagrxu 46u IPv4 ... TCP 127.0.0.1:52618 (LISTEN)
```

端口 52618 由 Kiro Helper (PID 98719) 监听 → server 活着。

于是我写了个最小 JSON-RPC 客户端 `/tmp/omni_probe.js` 复现 proxy 的握手，
枚举工具列表：

```bash
$ node omni_probe.js <langgraph-workspace> 52618
```

成功返回 **16 个工具**。然后用**本项目路径**再试一次：

```bash
$ node omni_probe.js /Users/yagrxu/me/collaborations/2026/tfc/self-evolve-demo-with-omni 52618
VERIFY ERR {"code":-32001,"message":"Workspace mismatch: extension serves
  \".../agents/langgraph\" but client requested \".../self-evolve-demo-with-omni\".
  Is the extension running for this workspace?"}
```

> ### 🔴 决策点 R1
> **Omni MCP 一次只服务一个 workspace。** 本项目要用 MCP，必须让 Kiro 打开本项目目录。
>
> **应对**：把工程拆成两类工作 ——
> - **不依赖 MCP** 的（agent 代码、dataset、CDK、脚本）先全部写完；
> - **依赖 MCP** 的（本地 trace 验证、dataset 注册、evaluator 运行、A/B replay）攒到最后，
>   让用户只需切一次 Kiro workspace。
>
> 同时把探针脚本产品化为 `scripts/omni.mjs`（见 Phase 0.4）——这样流量脚本 / 导出脚本
> 能在纯 shell 或 CI 里调 Omni 工具，不必依赖 IDE 里的 agent 会话。

### -1.4 枚举的 16 个工具

| 工具 | 用途 | 本 Demo 用在 |
|---|---|---|
| `check_credentials` | AWS 凭证 doctor | Phase 2/3 前置检查 |
| `discover_agent_traces_metadata` | 云端 trace 存储的**真实列名**（FIELDS/VALUES 双模式） | Phase 4 写 SQL 前必调 |
| `search_agent_traces` | 云端 trace 查询（裸 DataFusion SQL） | Phase 3/4 |
| `search_local_telemetry` | 本地 `.omni/traces.jsonl`（list/get/delete，无 SQL） | Phase 1/2/5 |
| `manage_datasets` | dataset CRUD + 不可变版本 | Phase 2/4 |
| `manage_evaluations` | evaluator 列举/运行/读结果/自建 | Phase 2/3/5/6 |
| `manage_test_agent` | 本地 agent 配置 + invoke + ping | Phase 1/2/5 |
| `manage_annotations` | trace 标注 | Phase 4 标"差"trace |
| `local_server` | dev server 生命周期 | Phase 1 |
| `manage_local_collector` | 本地 OTLP collector | Phase 1 |
| `configure_omni` | 项目配置 / prompt_setup_complete | Phase 1 |
| `get_invocation_graph` | 云端调用图 | Phase 3 可视化 |
| `setup_skills` | 装 skill 到 coding agent | Phase 0 |
| `get_context` / `notify_ui` / `show_credentials_dialog` | IDE 交互 | 按需 |

### -1.5 实调验证（不是只看 schema）

**`check_credentials`**：

```bash
$ node omni_call.js check_credentials '{}'
{"session_scoped": true, "can_sign": true, "ready": true,
 "credential_source": "default-chain",
 "caller_identity": {"account": "613477150601",
                     "arn": "arn:aws:iam::613477150601:user/yagrxu"},
 "auth_status": "ready"}
```

→ ✅ default profile 可用，且**已通过 Omni 后端授权**（`ready:true` 不只是 STS 能签名）。

**`manage_evaluations(list_evaluators)`** → 返回 20+ 个托管 evaluator：

| 层级 | Evaluator |
|---|---|
| TRACE | Correctness, Faithfulness, Helpfulness, ResponseRelevance, Conciseness, Coherence, InstructionFollowing, Refusal, Harmfulness, Stereotyping, ThirdParty.DeepEval.Bias, ThirdParty.DeepEval.Toxicity |
| TOOL_CALL | ToolSelectionAccuracy, ToolParameterAccuracy, SkillSelectionAccuracy, SkillInstructionFollowing |
| SESSION | GoalSuccessRate, TrajectoryExactOrderMatch, TrajectoryInOrderMatch, TrajectoryAnyOrderMatch |

→ ✅ 足够组出多维评分；且 `create_evaluator` 支持自建 LLM-judge（自定义 instructions +
rating_scale + model_id）→ 我们的 `PolicyGrounding` 有地方落。

**`discover_agent_traces_metadata`**：

```bash
$ node omni_call.js discover_agent_traces_metadata '{}'
{"dataStore": "default", "sampled": false, "keys": [], "totalKeys": 0, "pageSize": 25}
```

→ ✅ 通路正常；`totalKeys:0` 是因为云端当前窗口确实没有 trace（还没部署任何东西）。
这也顺便验证了 skill 里的警告："0 rows 意味着列不存在，不是没数据" —— 这里是整个 store 空。

### -1.6 从 schema 里抽出的**易踩坑点**（写代码时必须遵守）

1. **`search_agent_traces` 的时间边界**：必须 `BETWEEN to_timestamp(<整数秒>) AND to_timestamp(<整数秒>)`，
   **两端都要绑定**。开放/缺失/过宽的上界会被直接拒（"exceeds 365 days"），不是裁剪 —— 白白浪费一次往返。
   起始窗口用 1 小时，空了才放宽。
2. **traces/logs/metrics 同一张表**：每条 query 必须
   `WHERE (<自己的条件>) AND "@telemetry_type" = 'traces'`，
   而且**自己的条件要括起来** —— `AND` 优先级高于 `OR`，不括就会漏出 logs。
3. **没有 `service.name` 列**：agent 名是 `attributes.agent.name`；session 是 `attributes.session.id`。
4. **单位陷阱**：`search_local_telemetry` 的 window 是**毫秒**，`search_agent_traces` 的
   `to_timestamp()` 是**秒**。两边搞混会静默返回 0 行。
5. **`manage_datasets` 每个 action 都必须带 `dataSource`**（`local` / `cloud`）。
6. **dataset 名字规则**：`[a-zA-Z][a-zA-Z0-9_]{0,47}` —— 不能有连字符。
7. **`manage_evaluations(results)` 不跑 evaluator**，它只是读已经发出的 evaluation span；
   要打分必须用 `action: "run"`。

### -1.7 Skill 验证（21 个，通读关键 3 个）

```bash
$ ls .../agents/langgraph/.kiro/skills/
omni-adot-instrumentation      omni-instrument-vercel-ai     omni-production-observability
omni-audit                     omni-instrumentation          omni-prompt-setup
omni-context-awareness         omni-instrument-crewai        omni-self-evolution
omni-dev-server-error          omni-instrument-langchain     omni-test-agent-invocation
omni-diagnose-instrumentation-failure  omni-instrument-langgraph  omni-trace-analysis
omni-features                  omni-instrument-openai-agents omni-workflows
omni-openinference-framework-guide     omni-instrument-strands
```

**`omni-self-evolution`（283 行）—— 本 Demo 第 4~5 步的核心。** 它是一份
"PROCEDURAL SCRIPT"，11 步，关键约束：

| 约束 | 值/含义 |
|---|---|
| 云端 trace 访问 | **只读**；默认不建 cloud dataset |
| `min_eligible_examples` | 12（不够就 `NO_DECISION`，不许放宽） |
| `holdout_fraction` | 0.30，按 `scenario_id` 稳定 hash 切分 |
| `min_holdout_examples` / `min_control_examples` | 5 / 5 |
| `replay_repetitions` | 3 |
| `min_quality_delta` | 0.03（paired holdout/control 提升下限） |
| bootstrap CI | 0.95，**下界必须 ≥ 0** |
| `max_evaluator_regression` | 单 evaluator 回退 ≤ 0.02 |
| `max_latency_regression_pct` / `max_token_regression_pct` | 10 / 10 |
| safety 硬门 | harmfulness / PII / security 必须过，且零新增失败 |
| 允许的变更 | **仅 prompt**，且 model/tools/code/dataset 必须冻结 |
| 禁止 | 发布 prompt、改 canary、动 SSM、部署代码、改任何生产资源 |
| 产物 | `.omni/self-evolution/<run_id>/`（manifest / 三份 dataset / candidates / runs / decision.json / report.md） |
| 结论枚举 | `WINNER` / `NO_CHANGE` / `NO_DECISION` / `ROLLED_BACK_LOCAL` |

> **重要推论**：skill 要求"变体必须由显式 version/override 隔离，且 trace 里能验证 prompt version/hash"。
> 这意味着 **prompt 必须先被 Omni 托管**（`prompts.json` + loader + `OmniPromptProcessor`），
> 否则 A/B replay 无从谈起。→ 所以 `omni-prompt-setup` 必须在 Phase 1 就做，不能推后。

**`omni-prompt-setup`（480 行）** —— 给出了 `prompt_loader.py` 的完整实现：
`OMNI_PROMPTS_OVERRIDE` 环境变量可整体换 prompts 文件（**这就是我们做变体隔离的钩子**），
`get_prompt()` 把 template+version 写进 ContextVar，`OmniPromptProcessor`（一个 SpanProcessor）
在 span 上打 `llm.prompt_template.template` / `llm.prompt_template.version`。
明确警告：只生成 `get_model_config()` 不够，**必须真的替换硬编码的模型实例化**，
否则 prompts.json 里的 `model` 字段是装饰性的。

**`omni-test-agent-invocation`（814 行）** —— AgentCore 协议契约：

```
preset AgentCore: endpoint /invocations
  headers: Content-Type: application/json, Accept: text/event-stream,
           X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: {{sessionId}}
  body:    {"prompt":"{{userPrompt}}","sessionId":"{{sessionId}}"}
```

以及 AgentCore Python 项目的启动命令是 `agentcore dev -l --skip-deploy`
（**不要**前置 `source .venv/bin/activate`，会和它自管的 venv 冲突）。

**`omni-production-observability`（83 行）** —— 部署到云上要满足：

```bash
aws xray update-trace-segment-destination --destination CloudWatchLogs --region <region>  # 每账号一次
```
+ runtime env `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`。

### -1.8 AWS 侧就绪度实测

```bash
$ aws sts get-caller-identity
{"Account": "613477150601", "Arn": "arn:aws:iam::613477150601:user/yagrxu"}

$ aws configure get region
ap-southeast-1
```

> ### 🟡 决策点 R4
> profile 默认 region 是 **ap-southeast-1**，但 AgentCore 资源和 Omni skill 的默认都在 **us-east-1**。
> **所有代码 / CDK / 脚本一律显式写 `us-east-1`**，绝不依赖 profile 默认值 ——
> 否则会静默连到空 region，查不到任何 trace，且极难 debug。

```bash
$ aws xray get-trace-segment-destination --region us-east-1
{"Destination": "CloudWatchLogs", "Status": "ACTIVE"}          # ✅ 前置条件已满足，无需再执行 update

$ aws xray get-indexing-rules --region us-east-1
{"IndexingRules": [{"Name": "Default", "Rule": {"Probabilistic": {"DesiredSamplingPercentage": 100.0}}}]}
```

→ ✅ **X-Ray → CloudWatch Logs 已 ACTIVE，100% 采样**。生产 trace 会落进来。

```bash
$ aws bedrock-agentcore-control list-agent-runtimes --region us-east-1
cat_demo_strands-On0FF98ser     READY   v2
cat_demo_langgraph-r7Zqtf9sUO   READY   v2
```

> ### 🟡 决策点 R5
> 账号里**已有两个别人的 demo runtime**。所有新资源统一加 `selfevolve-demo` 前缀，
> 放独立 CDK stack，**只 create，绝不 touch 已有资源**。

参考已有 runtime 的配置形态（用于对齐我们的 CDK）：

```bash
$ aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id cat_demo_strands-On0FF98ser --region us-east-1
{
  "networkConfiguration": {"networkMode": "PUBLIC"},
  "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
  "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "...dkr.ecr...:5c7c11d5"}},
  "environmentVariables": {"MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0", ...},
  "metadataConfiguration": {"requireMMDSV2": true}
}
```

（注意它用的是 **container**；我们要走 **code** 路径，见下。）

```bash
$ aws bedrock list-foundation-models --region us-east-1 --query 'modelSummaries[?contains(modelId,`claude`)].modelId'
anthropic.claude-haiku-4-5-20251001-v1:0
anthropic.claude-sonnet-4-5-20250929-v1:0
anthropic.claude-sonnet-5
anthropic.claude-opus-5
... (13 个)
```

→ ✅ Haiku 4.5（v1 故意用的弱模型）和 Sonnet 4.5（Phase 5 "换模型"这一优化轴）都在。

### -1.9 CloudFormation / CDK 对 AgentCore 的支持

```bash
$ aws cloudformation list-types --visibility PUBLIC --type RESOURCE \
    --filters Category=AWS_TYPES --region us-east-1 --query 'TypeSummaries[].TypeName' \
    --output text | tr '\t' '\n' | grep -i agentcore
AWS::BedrockAgentCore::Runtime
AWS::BedrockAgentCore::RuntimeEndpoint
AWS::BedrockAgentCore::Dataset
AWS::BedrockAgentCore::Evaluator
AWS::BedrockAgentCore::OnlineEvaluationConfig
AWS::BedrockAgentCore::Gateway / Memory / Policy / ... (共 33 个)
```

→ ✅ **Runtime、Evaluator、OnlineEvaluationConfig 都能用 CloudFormation 声明**
（也就是能进 CDK）。这意味着"自定义 evaluator + 云上持续评估"可以和 agent 一起
`cdk deploy`，不用手工点控制台 —— 对可追溯性和可重放性是关键。

```bash
$ npm i aws-cdk-lib && ls node_modules/aws-cdk-lib/ | grep -i agentcore
aws-bedrockagentcore

$ grep -oE "declare class Cfn[A-Za-z]+" .../aws-bedrockagentcore/lib/bedrockagentcore.generated.d.ts
CfnRuntime  CfnRuntimeEndpoint  CfnDataset  CfnEvaluator  CfnOnlineEvaluationConfig
CfnMemory   CfnGateway ... (25 个)

$ ls .../aws-bedrockagentcore/lib/evaluation/
custom-evaluator.d.ts  evaluator.d.ts  online-evaluation.d.ts  data-source.d.ts  types.d.ts ...
```

→ ✅ `aws-cdk-lib` **2.269.0** 同时提供 L1（`CfnRuntime` 等）和 **L2**（`evaluation/` 模块：
`OnlineEvaluationConfig`、`EvaluatorSelector.builtin()`、`BuiltinEvaluator` 枚举）。

### -1.10 关键发现：Direct Code Deploy（无需 Docker）

拉 `AWS::BedrockAgentCore::Runtime` 的完整 schema：

```bash
$ aws cloudformation describe-type --type RESOURCE \
    --type-name AWS::BedrockAgentCore::Runtime --region us-east-1 --query Schema --output text
```

关键片段：

```
required: ['AgentRuntimeName', 'AgentRuntimeArtifact', 'RoleArn']

AgentRuntimeArtifact:  { ContainerConfiguration | CodeConfiguration }

CodeConfiguration:                      # ← 无需 Docker 的路径
  required: [Code, Runtime, EntryPoint]
  Code:       { S3: { Bucket, Prefix, VersionId? } }
  Runtime:    PYTHON_3_10 | PYTHON_3_11 | PYTHON_3_12 | PYTHON_3_13 | PYTHON_3_14 | NODE_22
  EntryPoint: string[]   minItems 1, maxItems 2, insertionOrder true

ProtocolConfiguration: MCP | HTTP | A2A | AGUI
NetworkConfiguration:  { NetworkMode: PUBLIC | VPC }
LifecycleConfiguration: { IdleRuntimeSessionTimeout, MaxLifetime }
EnvironmentVariablesMap: maxProperties 50
```

**`EntryPoint` 上限 2 个元素**这一点很关键。检索内部资料确认了它的用法：
Amazon 内部已有生产服务（BooksWebsearch）就是这么部的 ——

> Entry point `opentelemetry-instrument agent.py`, Python 3.12,
> deployed as an S3 ZIP (Direct Code Deploy) built by the AgentRuntime component.
> Observability: OpenTelemetry → CloudWatch / X-Ray, with Transaction Search enabled.

也就是说 `EntryPoint: ["opentelemetry-instrument", "agent.py"]` 正好用满 2 个位置，
**ADOT 埋点通过 entry point 前缀注入，不需要改代码、不需要 Docker**。

另外 AgentCore 团队的 feature launch 培训里明确了依赖打包契约：

> "customers need to give us the Python runtime version that they want and also
> **zip up their dependencies corresponding to that specific Python runtime version**"

> ### 🟢 决策：走 CodeConfiguration + S3 zip
> - 无需本地 Docker，`cdk deploy` 更快（Demo 里改 prompt 后重部署是高频操作）
> - `s3_assets.Asset` + bundling 就能产出 zip；asset hash 变化自动触发 runtime 新版本
> - **代价/风险 R2**：依赖必须 vendored 进 zip 且平台要对（见下）
>
> ### 🟡 决策点 R2
> 依赖打包的平台轮子（ARM64 vs x86_64）是唯一没法靠读文档确定的事。
> **Phase 3 开头先部一个最小 runtime 实测**（~10 分钟），跑通就继续；
> 跑不通自动回退 ContainerConfiguration + ECR（Dockerfile 一并准备好，不返工）。

### -1.11 Phase -1 结论

| 问题 | 结论 |
|---|---|
| Omni MCP 可用吗 | ✅ 可用，16 工具，关键 3 个已实调；⚠️ 绑定单一 workspace（R1） |
| Skill 流程可执行吗 | ✅ `omni-self-evolution` 是可直接执行的程序化脚本；前置依赖 `omni-prompt-setup` |
| Evaluator 够用吗 | ✅ 20+ 内置（三个层级）+ 可自建 LLM-judge |
| 云端可观测性就绪吗 | ✅ X-Ray→CWL ACTIVE，100% 采样，无需额外开通 |
| CDK 能部 AgentCore 吗 | ✅ L1+L2 齐备，Runtime/Evaluator/OnlineEvaluation 都能声明式部署 |
| 能免 Docker 部署吗 | ✅ CodeConfiguration + S3 zip + `["opentelemetry-instrument","agent.py"]`（R2 待实测） |
| 有哪些坑 | region 陷阱（R4）、毫秒/秒单位陷阱、SQL 时间边界与 telemetry_type 括号、已有 runtime 勿动（R5） |

**→ 全流程可行，进入 Phase 0。**

---

<a name="phase-0"></a>
## Phase 0 — 项目骨架与 Omni 接线

### 0.1 骨架

```bash
$ git init -q
$ mkdir -p agent/fixtures datasets evaluators cdk/{bin,lib} scripts reports docs .kiro/skills
```

### 0.2 `.mcp.json`

把 Omni MCP server 指向**本 workspace**（而不是用户原来给的 langgraph 路径）。
`autoApprove` 里只放只读/幂等的工具 —— `manage_datasets`、`manage_evaluations`
这类会产生资源或花钱的故意不放，保留人工确认。

### 0.3 `.gitignore`

`.omni/` 不入版本库。这不是洁癖，是 `omni-self-evolution` skill 的明确要求：

> Local artifacts belong under `.omni/self-evolution/`, which remains **outside
> source control because traces can contain customer payloads**.

同时排除 `agent/.build/`（vendored 依赖，几十 MB）和 `runs/`（原始流量 dump）。

### 0.4 `scripts/omni.mjs` —— MCP 桥

把 Phase -1 的探针产品化。它复现 `mcp-proxy.js` 的三步握手
（`workspace/verify` → `initialize` → `tools/call`），但做成一次性 CLI，
于是流量脚本 / 导出脚本能在纯 shell 或 CI 里调 Omni 工具，不必依赖 IDE 会话。

额外做了一件事：**把 MCP 的 content 信封剥掉**。Omni 的工具把真正的 payload
放在 `content[0].text` 里的一段 JSON 字符串，直接看原始返回很难读。

两条路径都实测过：

```bash
# 路径 A：本 workspace 还没被 Omni 服务 → 期望给出可操作的报错
$ node scripts/omni.mjs call check_credentials '{}'
找不到 /Users/.../self-evolve-demo-with-omni/.omni/mcp-port
Omni 扩展没有为这个 workspace 运行。请在 Kiro 里打开：
  /Users/.../self-evolve-demo-with-omni
（Omni MCP 一次只服务一个 workspace — 见 docs/BUILD-LOG.md 决策点 R1）
exit=3

# 路径 B：指向 langgraph workspace → 验证脚本本身是对的
$ OMNI_WORKSPACE=.../agents/langgraph node scripts/omni.mjs call check_credentials '{}'
{ "can_sign": true, "ready": true, "credential_source": "default-chain", ... }
exit=0
```

退出码分了三档（1 协议/工具错、2 用法错、3 连不上/超时），方便后续脚本判断
"是我参数写错了"还是"Omni 没在跑"。

---

<a name="phase-1"></a>
## Phase 1 — v1 agent（故意做差）+ 本地埋点

### 1.1 先做 fixture，因为它决定了 oracle 的质量

顺序上先写 `agent/fixtures/`，不是先写 agent。理由：整条 self-evolution 链路的
可信度取决于"标注答案对不对"，而标注答案来自 fixture + 业务逻辑。

**`policies.json`** —— 5 个类目 + 3 条横切规则：

| 类目 | 退货窗口 | 折旧费 | 寄回运费 | 超窗口可换新 |
|---|---:|---:|---|---|
| electronics 电子产品 | 14 天 | 10% | 买家 | ✓ 30 天 |
| apparel 服饰 | 30 天 | 0% | **商家** | ✗ |
| books 图书 | 30 天 | 0% | 买家 | ✗ |
| home 家居 | 21 天 | 5% | 买家 | ✓ 30 天 |
| perishable 生鲜 | **0 天** | — | — | ✗ |

三条横切规则（都带 id，便于验证"agent 到底引了哪条"）：

- `COUPON_PRORATE_BY_LIST_PRICE` —— 订单级券按各商品**原价小计比例**分摊；
  退货只退分摊后实付，不退整张券面值。舍入差额补给原价最大的那件，保证分摊总额分毫不差。
- `SHIPPING_REFUND_RULE` —— 原始运费只在「整单退 **且** 该品类运费由商家承担」时退。
- `DAMAGED_OVERRIDE` —— 到货破损无论是否超窗口都全额退，免折旧、运费商家承担。

**`orders.json`** —— 55 个订单，ID 分三段且**互不相交**：

| 区间 | 用途 |
|---|---|
| `ORD-100xx` (15) | `dev_local`，Phase 2 本地基线 |
| `ORD-101xx` (20) | `prod_sim`，Phase 3 云上流量模拟 |
| `ORD-102xx` (20) | `verify`，Phase 6 **held-out** 验证 |

> **为什么必须不相交**：Phase 6 要证明"提升是真的"。如果 verify 用了前面见过的
> 订单，那提升可能只是模型/prompt 记住了那几个金额。三段不相交，
> `scripts/build_datasets.py` 里有断言强制校验（见 1.4）。

### 1.2 决策：把"今天"钉死

`agent/tools.py` 里：

```python
def as_of() -> date:
    """本 Demo 的"今天"。固定值。"""
    return datetime.strptime(os.environ.get("DEMO_AS_OF_DATE", "2026-09-17"), "%Y-%m-%d").date()
```

> ### 🟢 决策：不用 `date.today()`
> 退货资格判定依赖"距下单多少天"。如果用真实当天，同一条 dataset example
> 明天跑就变成另一个答案 —— baseline 和 candidate 在不同时刻 replay 就**不可比**，
> A/B 结论直接失效。所以固定基准日，并把它同时注入云端 runtime 的环境变量
> （见 `cdk/lib/agent-stack.ts` 的 `DEMO_AS_OF_DATE`），保证本地和云上算出同一个 oracle。

### 1.3 工具层：业务逻辑是纯函数，工具只是薄包装

`agent/tools.py` 分两层：
- 纯函数：`allocate_coupon` / `item_verdict` / `compute_refund`
- 工具包装：`get_order` / `get_refund_policy` / `check_return_eligibility` /
  `calculate_refund` / `create_return_label` / `get_coupon_allocation`

> ### 🟢 决策：dataset 的 oracle 直接 import 这些纯函数
> `scripts/build_datasets.py` 用**同一份算法**渲染 `expected_response`。
> 手写标注答案的话，优惠券分摊这种算术几乎必错，而错的标注答案会让 evaluator
> 把正确回答判成错的 —— 那是最恶劣的评估污染，且极难发现。

**7 个原型逐一实测**（`item_refund` / `total_refund` 均为工具计算结果）：

| 订单 | 场景 | 结果 |
|---|---|---|
| ORD-10001 | electronics 5 天整单退，运费买家付 | 549.00 − 10%(54.90) = **494.10**；运费 12.00 不退 ✓ |
| ORD-10002 | electronics 23 天 | `EXCHANGE_ONLY`（超 14 天窗口，仍在 30 天换新窗口）✓ |
| ORD-10011 | 券 200 单品分摊 | 分摊 200.00，实付 1099.00 ✓ |
| ORD-10012 | 券 80 两品分摊 | 音箱 77.18% → 61.74；手机壳 22.82% → 18.26；合计 **80.00**（分毫不差）✓ |
| ORD-10012 | 只退 1 个手机壳（qty 2 中的 1） | 59.00 − 9.13(按件分摊) = 49.87 − 10%(4.99) = **44.88**；部分退不退运费 ✓ |
| ORD-10013 | home 破损 55 天（超 21 天窗口） | 折旧费 **0**，退 1580.00 + 运费 25.00 = **1605.00** ✓ |
| ORD-10014 | apparel 整单退，商家付运费 | 699.00 + 运费 **18.00** = 717.00 ✓ |

> `ORD-10012` 的 **44.88** 是整个 Demo 的招牌数字：它要求先按原价比例分摊券、
> 再按件拆分、再扣折旧费。没有工具的模型不可能算对。

### 1.4 施工中发现的真 bug：`create_return_label` 不确定

第一版用了 Python 内置 `hash()` 派生 RMA 单号。**这是错的** —— `str` 的 hash
受 `PYTHONHASHSEED` 随机化影响，跨进程不稳定，而 docstring 却承诺
"replay 多次得到同一个号"。Phase 5 的 paired replay 会在不同进程里跑 baseline
和 candidate，RMA 不一致就会被 evaluator 当成真实差异。

改用 `hashlib.blake2b`，并实测跨进程稳定：

```bash
$ for i in 1 2 3; do PYTHONHASHSEED=random python3 -c "...create_return_label('ORD-10012',['PC-TPU'])..."; done
RMA-10012-7632
RMA-10012-7632
RMA-10012-7632
```

同样的理由，`prompt_loader.get_prompt_hash()` 也用 blake2b —— skill 要求把
baseline prompt hash 冻结进 manifest 并在写回 winner 前校验文件未被改动，
那个校验依赖跨进程稳定的哈希。

### 1.5 Prompt 托管层

`agent/prompt_loader.py` + `agent/prompts.json`，遵循 `omni-prompt-setup` skill
的 schema（保持与 Omni Studio Prompt Management 面板兼容）。

两个机制是 Phase 5 的前提，不是可选项：

1. **`OMNI_PROMPTS_OVERRIDE`** —— 环境变量指向另一份 prompts.json。
   Phase 5 为每个 candidate 生成一份独立文件，用不同 env 启动同一份代码，
   于是"除了 prompt 什么都没变"是**结构性保证**，不是靠人守规矩。
2. **`OmniPromptProcessor`**（SpanProcessor）—— 在每个 span 上打
   `llm.prompt_template.version` / `.template`。skill 说得很直接：
   trace 里没有 prompt 版本 → replay 结果无法归因 → `NO_DECISION`。

比 skill 参考实现多加的：`get_messages()`（agent 需要完整消息列表而非只要
system 文本）和 `get_prompt_hash()`（上面 1.4 说的冻结校验依据）。

### 1.6 v1 prompt：故意做差

```json
"content": "你是一个电商平台的售后客服助手。请友好、简洁地回答用户关于退换货的问题，让用户满意。"
```

模型 `us.anthropic.claude-haiku-4-5-20251001-v1:0`，temperature 0.3。

四处**刻意留下的缺陷**，各自对应一个可被 evaluator 抓住的失败模式：

| 缺的东西 | 会导致 | 被谁抓到 |
|---|---|---|
| 没要求"回答前必须查政策" | 凭常识说"30 天内都能退全款" | **PolicyGrounding**（自建） |
| 没给工具调用协议 | 漏调 `check_return_eligibility` | ToolSelectionAccuracy / TrajectoryInOrderMatch |
| 没给算术规则 | 自己算优惠券分摊，必错 | Correctness |
| 没要求"不可退时给替代方案" | 超窗口直接拒绝，不提可换新 | Helpfulness / GoalSuccessRate |
| "让用户满意"这句还有副作用 | 倾向于过度承诺 | Correctness / PolicyGrounding |

> ### 🟡 风险 R7 复核点
> "够差但不能崩"是有讲究的：如果 v1 差到答不出任何东西，evaluator 分数会贴地，
> 后续提升看起来像是从零开始，说服力反而弱。Phase 2 基线跑完后要看实际分数落点，
> 必要时微调 —— 目标区间是"明显有问题但仍是个能用的客服"。

### 1.7 Agent 主体

`agent/agent.py`：Strands `Agent` + `BedrockAgentCoreApp`，
`@app.entrypoint` 暴露 `/invocations`，请求体
`{"prompt": ..., "sessionId": ...}` —— 与 Omni 的 `AgentCore` preset 完全一致。

三条可变轴全部由**外部配置**驱动，代码零改动：

| 轴 | 开关 |
|---|---|
| prompt | `OMNI_PROMPTS_OVERRIDE` 指向不同 prompts.json |
| model | prompts.json 的 `model.modelId`（经 `get_model_config()` **真实生效**） |
| tools | `ENABLE_COUPON_TOOL=true` 追加 `get_coupon_allocation` |

两个容易做错、这里刻意处理了的点：

1. **模型必须真的从 prompts.json 读。** skill 专门警告过：只生成
   `get_model_config()` 却把模型写死，会让 prompts.json 的 `model` 字段变成纯装饰 ——
   在 Omni 面板里换模型看着生效了，运行时零影响。Phase 5 的"换模型"这条轴完全依赖这里。
   同时**保留原硬编码模型作为 fallback**，不丢原值。
2. **`OmniPromptProcessor` 是挂到已存在的 provider 上，不是自己新建一个。**
   ADOT 自动埋点（entryPoint 前缀 `opentelemetry-instrument`）会在业务代码之前
   建好 TracerProvider；自己再建一个会覆盖掉 ADOT 的导出管道，trace 就上不了云。
   所以走 `trace.get_tracer_provider()` → 解 `ProxyTracerProvider` 的包 →
   `add_span_processor`。

`invoke` 的返回里回显了 `prompt_version` / `prompt_hash` / `model_id` /
`tools_enabled` —— 这是变体身份的**第二重证据**，万一 trace 属性缺失，
流量脚本仍能从响应体确认"这条到底跑的哪个变体"。

### 1.8 三份 dataset 生成

`scripts/build_datasets.py`。15 个场景原型（窗口内退款 / 超窗口换新 /
超两窗口保修 / 服饰免运费退 / 服饰超期 / 图书 / 家居 / 家居换新 / 生鲜不可退 /
生鲜破损 / 券单品 / 券多品部分退 / 破损超窗口覆盖 / 多件部分退 / 混类目）。

用户提问刻意写成真实口吻 —— 不提类目、不提政策、不给天数：

> 「订单号 ORD-10012。我只想退一个手机壳，音箱留着，能退多少钱？」

这样 agent 必须自己查工具才能答对，弱 prompt 一定翻车。

```bash
$ python3 scripts/build_datasets.py
dev_local     15 条  订单 15 个  assertions 101 条
prod_sim      20 条  订单 20 个  assertions 140 条
verify        20 条  订单 20 个  assertions 135 条
✓ prod_sim∩verify = ∅
✓ dev_local∩verify = ∅
✓ dev_local∩prod_sim = ∅
✓ 全部校验通过
```

抽查最难的 `dev_012`（券 80 + 多品 + 部分退），15 条 assertion 全部是可核查的事实：

```
必须说明优惠券 80.00 元按原价比例分摊，而不是整张券退还或直接忽略
「手机保护壳 透明款」分摊到的券额必须为 18.26 元
「手机保护壳 透明款」的折旧费必须为 4.99 元
合计退款金额必须为 44.88 元
必须说明原始运费 15.00 元不退还
只能计算用户指定退回的商品（PC-TPU），不得把整单都算进退款
```

期间修了一处：同类目多件商品会生成重复的政策 assertion（"退货窗口为 14 天"出现两次），
去重后 398 → 376 条，避免同一检查被 evaluator 重复计权。

### 1.9 自定义 evaluator：`PolicyGrounding`

`evaluators/policy_grounding.json` 是**唯一真相来源** —— Phase 2 本地
（`manage_evaluations create_evaluator`）和 Phase 3 云上（CDK `agentcore.Evaluator`）
都读它。两边必须是同一份 rubric，否则本地 A/B 选出的 winner 和云上验证的分数
不在同一把尺子上，整条链路的结论就断了。

> ### 🟢 为什么内置 evaluator 不够
> `Correctness` 只看最终数字对不对；`InstructionFollowing` 只看有没有遵循
> system prompt —— 但 **v1 的 system prompt 本身就没要求查政策**，
> 所以它"遵循得很好"却答得很错。
> `PolicyGrounding` 问一个正交的问题：回答里的政策数值，是从 `get_refund_policy`
> 的返回里来的，还是编的？
>
> 最关键的一条判定原则：**没调政策工具却说出具体数值 = 编造，即使蒙对也算。**
> 因为它不是被证实的，只是碰巧 —— 而碰巧不可复现。

0–3 分制，并按 skill 要求**在 replay 之前就冻结**打分语义：

```json
"_scoring_semantics": {
  "direction": "HIGHER_IS_BETTER", "min": 0, "max": 3,
  "pass_threshold": 2, "canonicalization": "(score - 0) / (3 - 0)"
}
```

judge 模型用 Haiku 4.5 控成本：9 evaluator × 55 example × 多轮，判官调用量
远大于 agent 本身，而"比对数值是否一致"这个任务不需要更强的模型。

### 1.10 CDK：AgentCore Runtime + 云端评估

`cdk/lib/agent-stack.ts` + `cdk/lib/eval-stack.ts`。

**用 L2 而不是 L1。** 探查 `aws-cdk-lib/aws-bedrockagentcore/lib/` 时发现除了
generated 的 L1，还有成套 L2：

```
runtime/{runtime,runtime-artifact,runtime-endpoint,observability}.d.ts
evaluation/{evaluator,custom-evaluator,online-evaluation,data-source,types}.d.ts
```

关键 API：

```ts
AgentRuntimeArtifact.fromCodeAsset({ path, runtime: AgentCoreRuntime.of('PYTHON_3_12'),
                                     entrypoint: ['opentelemetry-instrument', 'agent.py'] })
DataSourceConfig.fromAgentRuntimeEndpoint(runtime, endpoint)
EvaluatorSelector.builtin(BuiltinEvaluator.CORRECTNESS)
EvaluatorConfig.llmAsAJudge({ instructions, modelId, ratingScale })
```

**编译期踩到的 4 个坑**（都是我先按直觉猜名字、被 `tsc` / `cdk synth` 打回来的）：

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| 1 | `UnsupportedFeatureFlag '@aws-cdk/core:enableStackNameDuplicates'` | 我从旧模板抄了 3 个 CDK **v1** 时代的 feature flag | 从 cdk.json 删掉 |
| 2 | `Export name must only include alphanumeric characters, colons, or hyphens (got 'selfevolve_demo_order_support-runtime-id')` | CFN 导出名不许下划线，但 **AgentCore runtime 名必须用下划线** —— 两套命名规则直接冲突 | 导出名改用带连字符的 `PREFIX`，runtime 名保持下划线 |
| 3 | `Description ... does not match pattern` (F3031) | IAM Role 的 Description 限定 ASCII/Latin-1，我写了中文 | 只有 role description 改英文，其余描述照常中文 |
| 4 | `Tags.0.Value 'cat_demo_strands,cat_demo_langgraph' does not match pattern` | IAM tag value 字符集 `^[\p{L}\p{Z}\p{N}_.:/=+\-@]*$` **不含逗号** | 分隔符换成 `/` |

另外把 L2 属性名猜错了两处，`tsc` 直接报出来：`Runtime` 的属性是
`agentRuntimeArn` / `agentRuntimeId`（不是 `runtimeArn`/`runtimeId`）；
endpoint 应该用 `runtime.addEndpoint(name, opts)` 而不是 `new RuntimeEndpoint({runtime})`。

**最终 `cdk synth` 零告警**，模板正是想要的：

```yaml
AWS::BedrockAgentCore::Runtime:
  AgentRuntimeArtifact:
    CodeConfiguration:
      Code: { S3: { Bucket: cdk-hnb659fds-assets-613477150601-us-east-1, Prefix: <hash>.zip } }
      EntryPoint: [opentelemetry-instrument, agent.py]     # ← ADOT 埋点，无需 Docker
      Runtime: PYTHON_3_12
  ProtocolConfiguration: HTTP
  NetworkConfiguration: { NetworkMode: PUBLIC }
  EnvironmentVariables:
    AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT: "true"           # ← skill 要求
    AGENT_OBSERVABILITY_ENABLED: "true"
    DEMO_AS_OF_DATE: "2026-09-17"                          # ← 固定"今天"
    AGENT_PROMPT_VARIANT: v1-baseline
    ENABLE_COUPON_TOOL: "false"
```

**`tracingEnabled: true` 这一行换来了整条 trace 投递管道**，全部声明式：

```
AWS::Logs::DeliverySource (LogType: TRACES, ResourceArn: <runtime>)
  → AWS::Logs::DeliveryDestination (DeliveryDestinationType: XRAY)
    → AWS::Logs::Delivery
+ AWS::XRay::ResourcePolicy
```

`RuntimeEndpoint` 通过 `Fn::GetAtt Runtime.AgentRuntimeVersion` 绑定版本 ——
所以**改 prompt → asset hash 变 → runtime 出新版本 → endpoint 自动跟上**，
这正是 Phase 6 重新部署所依赖的机制。

EvalStack 综合体：

```yaml
AWS::BedrockAgentCore::Evaluator:
  EvaluatorName: selfevolve_demo_PolicyGrounding
  Level: TRACE
  EvaluatorConfig.LlmAsAJudge: { ModelId: us.anthropic.claude-haiku-4-5..., RatingScale.Numerical: 4 档 }

AWS::BedrockAgentCore::OnlineEvaluationConfig:
  Evaluators: [Correctness, InstructionFollowing, Helpfulness, Faithfulness,
               ToolSelectionAccuracy, ToolParameterAccuracy, GoalSuccessRate,
               <PolicyGrounding GetAtt>]          # 8 个（上限 10）
  Rule.SamplingConfig.SamplingPercentage: 100     # Demo 要每条都有分
  ExecutionStatus: ENABLED
```

> **待 Phase 3 实测确认**：`DataSourceConfig.fromAgentRuntimeEndpoint` 推导出的
> log group 是 `/aws/bedrock-agentcore/runtimes/<runtimeId>-v1`，
> serviceName 是 `selfevolve_demo_order_support.v1`。这是 L2 的推导规则，
> 真实命名要部署后核对；不符就改用显式 `DataSourceConfig.fromCloudWatchLogs`。

### 1.11 打包脚本

`scripts/build_agent_bundle.sh`。托管 Python 运行时**不会**替你 pip install，
依赖必须已经在 zip 里（AgentCore 团队 feature launch 培训原话：
"zip up their dependencies corresponding to that specific Python runtime version"）。

脚本做 4 件事：复制源码+fixture → `pip install --target` → 删
`__pycache__`/`dist-info`/`tests` 精简体积 → 冒烟检查关键文件和依赖真的落盘了。

> ### 🟡 R2 仍未闭环
> `--platform` 默认 `manylinux2014_aarch64`，但**AgentCore 托管代码运行时跑在哪个
> CPU 架构，文档没有明确说明**，而带 C 扩展的包（pydantic-core / cryptography /
> grpcio）的 wheel 是分架构的。做成 `AGENT_BUNDLE_PLATFORM` 可切换，
> Phase 3 开头用最小 runtime 实测后写回本文档。
>
> 脚本里还留了一个检查：`bin/opentelemetry-instrument` 必须真的在包内
> —— 它是 entryPoint 的第一个元素，不在就启动不了。

### 1.12 环境阻塞：PyPI 只有 40 KB/s

本地 venv 装依赖异常慢，一开始以为卡死了。排查：

```bash
$ curl -s -o /dev/null -w "%{http_code} %{speed_download} B/s\n" \
    https://files.pythonhosted.org/packages/.../botocore-1.43.96-py3-none-any.whl
200 40974 B/s
```

**不是卡死，是 PyPI CDN 到本机只有 ~40 KB/s。** botocore 15.1 MiB +
grpcio 11.8 MiB 光这两个就要十几分钟。依赖解析本身是成功的
（`bedrock-agentcore==1.23.1`、`aws-opentelemetry-distro==0.19.0`），
只是下载慢。

顺带发现 `grpcio` 是被 `aws-opentelemetry-distro` 的
`opentelemetry-exporter-otlp-proto-grpc` 拖进来的 —— 我们只用 http/protobuf，
这 12 MB 是纯浪费，但它是硬依赖，无法在不 patch 上游的前提下剔掉。
影响：本地安装慢 + 部署包变大（冷启动略慢），功能无影响。

### 1.13 依赖装完，验证 agent 真的能跑

```bash
$ .venv/bin/python -c "import importlib.metadata as m; ..."
strands-agents                   1.56.0
bedrock-agentcore                1.23.1
aws-opentelemetry-distro         0.19.0
boto3                            1.43.96
```

导入冒烟：

```
prompt version: order_support-v1
prompt hash   : c76023c4c730bed0
model config  : {'providerId':'bedrock','modelId':'us.anthropic.claude-haiku-4-5-...','parameters':{'temperature':0.3,'max_tokens':1024}}
agent.py OK | tools: [get_order, get_refund_policy, check_return_eligibility, calculate_refund, create_return_label]
```

### 1.14 埋点的真问题：裸 `python agent.py` 挂不上 SpanProcessor

冒烟时我自己写的告警就打出来了：

```
WARNING:order-support:TracerProvider (ProxyTracerProvider) 不支持 add_span_processor
  —— trace 上不会有 llm.prompt_template.version，A/B replay 将无法归因变体。
processor registered: False
```

对照实验，同一份代码换个启动方式：

```bash
$ .venv/bin/opentelemetry-instrument .venv/bin/python -c "..."
INFO:order-support:OmniPromptProcessor 已注册到 TracerProvider
provider type: TracerProvider     has add_span_processor: True
processor registered: True
```

**根因**：不走 `opentelemetry-instrument` 时没有任何 SDK provider 被安装，
`trace.get_tracer_provider()` 返回的是 `ProxyTracerProvider`，它没有
`add_span_processor`。而这个版本的 `ProxyTracerProvider` 既没有 `get_delegate()`
也没有 `_delegate`，我原来的解包逻辑两个都试不出来。

> ### 🟢 两处修改
> 1. **`_register_prompt_processor` → `ensure_prompt_processor()`**：改成幂等 +
>    **每次 invoke 惰性重试**。因为 ProxyTracerProvider 是会被后来的
>    `set_tracer_provider` 替换掉的 —— 加载顺序稍有不同就永久丢掉版本属性，
>    而那会让整个 A/B 结论作废，不能靠"启动顺序恰好对"来保证。
> 2. **本地启动命令必须带 `opentelemetry-instrument`**，与云端 entryPoint 一致：
>    `cd agent && ../.venv/bin/opentelemetry-instrument ../.venv/bin/python agent.py`
>
> 另外在 invoke 的返回里加了 `prompt_attrs_on_trace: bool` —— 明确告诉调用方
> trace 上到底有没有版本属性。为 false 时必须退回用响应体里回显的
> `prompt_version`/`prompt_hash` 归因，**不让它静默失败**。

端到端验证（真实 Bedrock 调用）：

```bash
$ curl -s http://localhost:8080/ping
{"status":"Healthy","time_of_last_update":1789657478}

$ curl -X POST .../invocations -d '{"prompt":"订单号 ORD-10012。我只想退一个手机壳…"}'
prompt_version : order_support-v1
model_id       : us.anthropic.claude-haiku-4-5-20251001-v1:0
attrs_on_trace : True          ← 埋点通了
```

### 1.15 🔴 发现设计缺陷：**工具太强，导致 v1 一点都不弱**

拿上面那次调用的实际回答一看，问题就暴露了：

```
您退1个手机壳（PC-TPU）可以退 ¥44.88
- 手机壳原价：¥59.00   - 分摊的优惠券：-¥9.13
- 实付金额：¥49.87     - 折旧费（10%）：-¥4.99
- 最终退款：¥44.88     ← 完全正确
```

再补测 3 个本该翻车的场景（超窗口换新 / 破损覆盖 / 生鲜不可退 / 超两窗口保修）——
**v1 全部答对。**

**根因不是 prompt 太强，是我把工具做得太强了：**

| 工具 | 我原本的实现 | 后果 |
|---|---|---|
| `get_order` | 直接返回 `days_since_purchase` | 模型不用做日期减法 |
| `check_return_eligibility` | 返回**完整裁决** + `policy_basis` + 替代方案 | 模型不用套政策、不用判边界 |
| `calculate_refund` | 包办券分摊 / 按件拆分 / 折旧费 / 运费规则 | 模型不用做任何算术 |

于是 agent 只需要「调一次工具、把结果念出来」。四个刻意留在 prompt 里的缺陷
全部被工具兜住了 —— **整个 Demo 的前提"一开始效果不好"不成立。**

> ### 🟢 重设计：工具分两层
>
> 让 BASIC 层退回**真实后端该有的样子** —— 给原始字段，推理和算术留给模型。
> 计算类工具移到 ENHANCED 层，成为 Phase 5「加 tool」这条优化轴的**实际内容**。
>
> 这不只是修 bug，它让那条优化轴从装饰变成了真有效果的手段。
>
> | 层 | 工具 | 何时启用 |
> |---|---|---|
> | **BASIC**（默认） | `get_order`（原始字段，**不含**距今天数，只给 `order_date` + `today`）<br>`get_refund_policy`（原始政策字段 + 三条规则原文，**不给结论**）<br>`search_knowledge_base`（**干扰项**，见下）<br>`create_return_label` | baseline / c1 / c3 |
> | **ENHANCED** | 上面 4 个 + `check_return_eligibility` + `calculate_refund` + `get_coupon_allocation` | Phase 5 candidate c2 |
>
> 开关：`AGENT_TOOL_TIER=basic|enhanced`（默认 basic）。
> **纯 prompt 类 candidate（c1/c3）用的是和 baseline 完全相同的 4 个工具**，
> 满足 skill 要求的"除被测轴之外一切冻结"。
>
> **新增干扰项工具 `search_knowledge_base`**（`agent/fixtures/faq.json`）：
> 返回真实企业帮助中心那种「措辞友好但没有可执行数值」的 FAQ ——
> 比如"一般来说在收到商品后的一定期限内提出申请即可（通常为 7-15 天，部分品类更长）"。
> 它**没有说谎**，只是不精确、不分类目，真实企业 FAQ 就是这样，所以这个干扰是公平的、
> 不是刻意挖坑。弱 prompt 没规定"政策数值必须来自 `get_refund_policy`"，
> 模型很容易拿它当依据 —— 这正是 ToolSelectionAccuracy 和 PolicyGrounding 要抓的。

**重设计后重测同样的场景，v1 按预期翻车了**：

| 场景 | oracle | v1 实际输出 | 失败类型 |
|---|---|---|---|
| `dev_012` 券80+多品+部分退 | **¥44.88** | **¥73.33** | 用户说"只退一个手机壳"，它按 **2 件**算；且券分摊按单件算错 |
| `dev_008` home 28 天 | 28>21 天窗口 → **不可退**，30 天内可换新 | 「**✅ 可以退货**！…如果坚持退货，需要支付5%的折旧费（约¥23元）」 | **自相矛盾**（先说超期又说可退）+ **对不可退商品报出金额** → 触发确定性硬门 |
| `dev_013` 破损超窗口 | 1580 + 运费 25 = **1605** | 结论对，但**完全没给金额** | 不完整 |
| `dev_002` 超窗口换新 | 不可退、可换新 | 正确 | — |
| `dev_011` 券200 单品 | **¥989.10** | 正确 | — |

失败率约 40–60%，正好落在 **R7 的目标区间**："明显有问题但仍是个能用的客服"。

`dev_008` 是演示的招牌案例：agent 在同一段话里既承认超期又说可以退货，
还报了一个具体折旧费金额 —— 这在真实业务里就是一条客诉。

### 1.16 连带修正：`expected_trajectory` 必须按工具层分开

工具分层带来一个**评估公平性问题**，必须一起修掉：

dataset 原来的 `expected_trajectory` 写的是
`[get_order, get_refund_policy, check_return_eligibility, calculate_refund]`。
但后两个已经不在 BASIC 层 —— 那样 `TrajectoryInOrderMatch` 会
**因为工具根本不存在而 100% 失败**。

> ### 🟢 这不是模型的问题，是评估设计的问题
> 如果不修，baseline 的轨迹分会虚低到 0，Phase 5 的"提升"就被**系统性夸大**了 ——
> 而且这种夸大很难被发现，因为它看起来像是"candidate 真的改好了轨迹"。
>
> 修法：`expected_trajectory` 存 BASIC 层期望（`[get_order, get_refund_policy]`），
> ENHANCED 层期望放进 `metadata.expected_trajectory_enhanced`，
> 评估 c2 时替换使用。

同时修了 oracle 渲染器：它原本读 `order["days_since_purchase"]`，
而这个字段已从 `get_order` 移除；改成直接调纯函数 `T.days_since_purchase(order_id)`
—— oracle 要真值，模型才需要自己算。

重新生成后校验依旧全绿：

```
dev_local     15 条  订单 15 个  assertions 101 条
prod_sim      20 条  订单 20 个  assertions 140 条
verify        20 条  订单 20 个  assertions 135 条
✓ prod_sim∩verify = ∅   ✓ dev_local∩verify = ∅   ✓ dev_local∩prod_sim = ∅
```

### 1.17 Phase 1 当前状态

| 产物 | 状态 |
|---|---|
| `agent/fixtures/{policies,orders}.json` | ✅ 55 订单，5 类目 + 3 横切规则 |
| `agent/tools.py` | ✅ 7 个原型实测通过；修了 1 个确定性 bug；**已按 BASIC/ENHANCED 两层重设计** |
| `agent/prompt_loader.py` | ✅ 变体隔离 + 版本打点 + 稳定哈希 |
| `agent/prompts.json` | ✅ v1（故意做差），4 处缺陷各对应一个失败模式 |
| `agent/agent.py` | ✅ 端到端跑通（真实 Bedrock 调用）；埋点惰性重试已修；工具分层开关 `AGENT_TOOL_TIER` |
| `agent/fixtures/faq.json` | ✅ 干扰项 FAQ 知识库（7 条） |
| **v1 基线质量** | ✅ 实测失败率 40–60%，落在 R7 目标区间；`dev_008` 自相矛盾+违规报价是招牌案例 |
| `datasets/{dev_local,prod_sim,verify}.json` | ✅ 55 条 example / 376 条 assertion / 三段不相交已断言 / **轨迹按工具层分开** |
| `evaluators/policy_grounding.json` | ✅ 0–3 分制，打分语义已冻结 |
| `cdk/` | ✅ `tsc` 通过 + `cdk synth` **零告警**；模板已人工核对 |
| `scripts/{omni.mjs,build_datasets.py,build_agent_bundle.sh,traffic.py}` | ✅ 已写（omni.mjs 与 build_datasets.py 已实测） |

| `scripts/{omni_client.py,local_baseline.py}` | ✅ 已写；⏳ 待 Kiro 切 workspace 后实跑（R1） |

**已闭环的验证**：agent 本地真实调用通、埋点属性上得去、oracle 算得准、
v1 弱得恰到好处、CDK synth 零告警且模板人工核对过。

**下一步（需要 R1 解锁）**：在 Kiro 里打开本项目目录，然后
`python scripts/local_baseline.py` —— 注册 dataset、建 PolicyGrounding
evaluator、跑完 15 条、9 个 evaluator 打分，产出 `reports/01-local-baseline.md`。
