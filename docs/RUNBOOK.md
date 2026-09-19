# 调用流程手册（Runbook）

> **本文只记录实测跑通的调用。** 错误尝试与踩坑过程在 [BUILD-LOG.md](BUILD-LOG.md)，
> 方法论在 [`.kiro/skills/omni-self-evolution/SKILL.md`](../.kiro/skills/omni-self-evolution/SKILL.md)。
>
> 每条调用后面标注了**必须这样做的理由**。没有理由的参数可以照抄，
> 带 ⚠️ 的参数改错了**不会报错，只会静默给出错误结果**。

环境：macOS · AWS account `613477150601` · **region `us-west-2`**（全链路统一，见 BUILD-LOG 决策点 R4'）

---

## 0. 前置条件

```bash
# Omni MCP server 必须为**本项目**运行 —— 它一次只服务一个 workspace
cat .omni/mcp-port                       # 有输出说明 Kiro 正在服务本目录
lsof -nP -iTCP:$(cat .omni/mcp-port) -sTCP:LISTEN   # 应看到 Kiro Helper

# ⚠️ 云端 trace 查询需要扩展宿主进程 env 里有 AWS_REGION。
# mcp-proxy.js 不转发 env，所以只能从终端带 env 启动 Kiro：
AWS_REGION=us-west-2 open -na Kiro --args "$(pwd)"
```

```bash
# region 前置项（每个 account+region 一次；本项目实测两个 region 都已 ACTIVE）
aws xray get-trace-segment-destination --region us-west-2    # → CloudWatchLogs / ACTIVE
aws xray get-indexing-rules --region us-west-2               # → Probabilistic 100%
```

MCP 桥：`scripts/omni.mjs` 复现 `mcp-proxy.js` 的三步握手
（`workspace/verify` → `initialize` → `tools/call`）并剥掉 content 信封。
退出码：`1` 工具/协议错 · `2` 用法错 · `3` 连不上。

```bash
node scripts/omni.mjs list                       # 枚举工具
node scripts/omni.mjs call <tool> '<json-args>'
```

---

## 1. 本地基线（Phase 2）

`python scripts/local_baseline.py` 内部依次调用：

```jsonc
// 1.1 告诉 Omni 本项目的形态。注意数据流方向：是我们告诉 Omni，不是反过来
configure_omni      {"action":"set_project_details","project_path":".",
                     "framework":"strands","language":"python","platform":"agentcore"}
configure_omni      {"action":"prompt_setup_complete","project_path":".",
                     "promptsJsonPath":"agent/prompts.json"}

// 1.2 启动命令。⚠️ 必须带 opentelemetry-instrument
//     裸 `python agent.py` 时 get_tracer_provider() 返回 ProxyTracerProvider，
//     挂不上 SpanProcessor → trace 上没有 llm.prompt_template.version → 无法归因变体
// ⚠️ 必须显式钉 AWS_REGION：Omni 的 Local dev 默认 us-east-1
manage_test_agent   {"action":"set_start_command","project_path":".","port":8080,
                     "command":"cd agent && AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 ../.venv/bin/opentelemetry-instrument ../.venv/bin/python agent.py"}
manage_test_agent   {"action":"set_agent_schema","project_path":".","preset":"AgentCore"}

// 1.3 起 collector 与 dev server
manage_local_collector {"action":"start","project_path":"."}
local_server           {"action":"start"}
manage_test_agent      {"action":"ping","project_path":"."}      // 轮询直到 active:true

// 1.4 自定义 evaluator。⚠️ TRACE 级只允许这四个占位符：
//     context / assistant_turn / expected_response / system_instructions
//     （available_tools、tool_turn 是 TOOL_CALL 级的，用了会被直接拒）
manage_evaluations  {"action":"create_evaluator","project_path":".",
                     "name":"selfevolve_demo_PolicyGrounding","level":"TRACE",
                     "instructions":"…{context}…{assistant_turn}…",
                     "rating_scale":{"numerical":[{"value":3,"label":"…","definition":"…"}]},
                     "model_id":"us.anthropic.claude-haiku-4-5-20251001-v1:0"}
// → {"evaluatorId":"selfevolve_demo_PolicyGrounding-XXXX","status":"ACTIVE"}

// 1.5 读回真实值域并冻结。⚠️ 别信文档也别信直觉：
//     实测 Builtin.Helpfulness 是 [0,6] 七档，其余都是 [0,1]
manage_evaluations  {"action":"get_evaluator","evaluator_id":"Builtin.Helpfulness"}
// → ratingScale.numerical[].value

// 1.6 invoke。session_id 由我们指定，用于后续显式对齐
manage_test_agent   {"action":"invoke_agent","project_path":".",
                     "prompt":"…","session_id":"baseline-dev_001-…"}
// ⚠️ 失败形态是 HTTP 成功 + 错误信封 {"error","message","sessionId"}，
//    且 content 为空 —— 必须显式检查，否则失败会被记成成功

// 1.7 找 trace。⚠️ 两段式，且窗口要紧贴这次调用
//    `list` 只返回摘要（没有 session 字段），分页默认 50 条
search_local_telemetry {"operation":"list","project_path":".",
                        "window":{"start":<t0-5000>,"end":<now+5000>},"limit":50}
//    再 `get` 才能拿到 spans；我们传的值在 span 属性 session.id 上
//    （顶层 sessionId 是 Omni 自己生成的 session-<ms>，匹配不上）
search_local_telemetry {"operation":"get","project_path":".","traceIds":["<id>",…]}
// → [{id, sessionId, tokenUsage{totalTokens}, spans:[{metadata:{
//      "session.id":…, "llm.prompt_template.version":…}}]}]

// 1.8 建 dataset。⚠️ 顺序是先 invoke 再建，让 sourceTraceId 在创建时就写进 example
//    （update_examples 即使带对 exampleId 也会把最后一条追加而非替换）
manage_datasets     {"action":"create","dataSource":"local","project_path":".",
                     "name":"dev_local","description":"…",
                     "examples":[{"scenario_id":"dev_001","turns":[…],"assertions":[…],
                                  "metadata":{"sourceTraceId":"<id>"}}]}
manage_datasets     {"action":"create_version","dataSource":"local","project_path":".",
                     "datasetId":"<id>"}
manage_datasets     {"action":"get_examples","dataSource":"local","project_path":".",
                     "datasetId":"<id>","version":1}          // 读回核对条数

// 1.9 打分。⚠️ 必须用 action:"run"。action:"results" 只读已有的 evaluation span，
//     不触发任何评估 —— 拿它当打分会得到一张空表
manage_evaluations  {"action":"run","project_path":".",
                     "evaluatorId":"Builtin.Correctness","traceIds":[…],
                     "dataSource":"local","evaluatorLevel":"TRACE","datasetId":"<id>"}
// → {"results":[{"evaluatorId":…,"score":1,"explanation":…,"itemKey":"<traceId>"}]}
// ⚠️ score == -1 是 "unscored"（常见原因：span 有 error）。
//    一律记为未评分，**绝不当 0 分** —— 当 0 会凭空造出"质量很差"的结论
// ⚠️ 对齐用 itemKey（就是 traceId），不是数组下标
```

**SESSION 级 evaluator 的适用范围**：本地 dev server 的 collector `session.id`
只在启动时设一次（写在 `.env.omni`），所有调用共享一个 session ⇒ SESSION 级
evaluator 只判一次、再把同一分数复制给每个 itemKey（**n=1 伪装成 n=N**）。
**本地不要用它做 per-example 聚合。**云端每个请求有独立 `runtimeSessionId`，那里有效。

---

## 2. 部署（Phase 3）

```bash
# 2.1 打包依赖。托管运行时不会替你 pip install
AGENT_BUNDLE_PLATFORM=manylinux2014_aarch64 bash scripts/build_agent_bundle.sh
#     ⚠️ 绝不能删 *.dist-info —— ADOT 靠 entry points 发现 distro/configurator/
#        instrumentor，而 entry points 存在 dist-info/entry_points.txt 里。
#        删了不会启动失败，只会让 agent 正常回答但**零 trace**。
#        脚本里有硬门：可发现的 entry point 少于 3 就 exit 1（正常约 57 个）

# 2.2 部署。ARM64 wheel 实测可用，无需 Docker
cd cdk && AWS_REGION=us-west-2 npx cdk deploy selfevolve-demo-agent --require-approval never
cd cdk && AWS_REGION=us-west-2 npx cdk deploy selfevolve-demo-eval  --require-approval never
```

关键模板片段（`cdk synth` 产物，已人工核对）：

```yaml
AWS::BedrockAgentCore::Runtime:
  AgentRuntimeArtifact.CodeConfiguration:
    Code: {S3: {Bucket: cdk-hnb659fds-assets-<acct>-us-west-2, Prefix: <hash>.zip}}
    EntryPoint: [opentelemetry-instrument, agent.py]    # 上限 2 个元素，正好用满
    Runtime: PYTHON_3_12
  EnvironmentVariables:
    AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT: "true"
    AGENT_OBSERVABILITY_ENABLED: "true"
    DEMO_AS_OF_DATE: "2026-09-17"                       # 钉死"今天"，保证 oracle 可复现

AWS::BedrockAgentCore::OnlineEvaluationConfig:
  DataSourceConfig.CloudWatchLogs:                      # ⚠️ 属性名是 DataSourceConfig
    LogGroupNames: ["/aws/bedrock-agentcore/runtimes/<runtimeId>-v1"]
    ServiceNames:  ["<runtimeName>.v1"]                 # 实测与 X-Ray service graph 一致
  Rule:
    SamplingConfig.SamplingPercentage: 100
    SessionConfig.SessionTimeoutMinutes: 15             # ⚠️ 评估要等它过后才开始
  ExecutionStatus: ENABLED
```

```bash
# 2.3 核对
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id> --region us-west-2
# → {"status":"READY","entry":["opentelemetry-instrument","agent.py"],"rt":"PYTHON_3_12"}
```

---

## 3. 云上流量与评分（Phase 3）

```bash
python scripts/traffic.py --dataset prod_sim          # 先 --limit 2 --dry-run 验证
```

```python
# 核心调用。⚠️ runtimeSessionId 至少 33 字符，且把 scenario_id 编进去 ——
#    这是后续"哪条 trace 对应哪条标注答案"的唯一可靠依据（绝不用位置匹配）
boto3.client("bedrock-agentcore", region_name="us-west-2").invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:us-west-2:<acct>:runtime/<id>",
    qualifier="v1",
    runtimeSessionId="selfevolve-prod_sim-prod_001-<run_id>",
    contentType="application/json", accept="application/json",
    payload=json.dumps({"prompt": …, "sessionId": <同上>}).encode())
```

```bash
# 3.1 确认 trace 落库（不依赖 Omni 云端通路）
aws xray get-trace-summaries --region us-west-2 \
  --start-time <epoch_s> --end-time <epoch_s> \
  --filter-expression 'service(id(name: "<runtimeName>.v1"))' \
  --query 'length(TraceSummaries)'
#   ⚠️ service 名带 `.v1` 后缀；漏了会返回 0 而看起来像"没流量"

# 3.2 拉在线评估分数
python scripts/fetch_cloud_scores.py --run traffic-prod_sim-<id> --slack 7200 --wait 2400 --poll 180
```

评估结果 log group（前缀发现，名字含随机后缀，别写死）：
`/aws/bedrock-agentcore/evaluations/results/<onlineEvalConfigId>`

```jsonc
// ⚠️ 一条 log event 的 message 里是**多条 NDJSON**，逐行 parse
// ⚠️ 评分在 OTel 扁平点号属性里，不是 {"evaluatorId","score"} 嵌套结构
{"name":"gen_ai.evaluation.result",
 "traceId":"…","spanId":"…",
 "resource":{"attributes":{"service.name":"<runtimeName>.v1"}},
 "attributes":{"gen_ai.evaluation.name":"Builtin.ToolParameterAccuracy",
               "gen_ai.evaluation.score.value":1.0,
               "gen_ai.evaluation.explanation":"…",
               "session.id":"selfevolve-prod_sim-prod_001-<run_id>"}}
```

**等待条件必须是「覆盖到的 session 数」，不是「有没有事件」。**
在线评估逐步推进：实测流量结束后约 **22 分钟**才覆盖满 20 个 session
（`SessionTimeoutMinutes: 15` + 排队）。"有事件就返回"会产出一份覆盖率 1/20 的报告，
均值全是 1.000，看着正常而毫无统计意义。

---

## 4. 导出差 trace、构建样本集（Phase 4）

```bash
# 可传多个 --run 合并 scenario 池。⚠️ verify 永不进池（Phase 6 专用 held-out）
python scripts/export_bad_traces.py \
  --run traffic-prod_sim-<id> --run traffic-dev_local-<id>
```

- 失败模式：按 evaluator 的 `pass_threshold` 归一化后判定，产出 `failure_modes.json`
- 切分：`blake2b(scenario_id)` 落在最低 30% 进 holdout。**纯函数** ——
  重跑得到完全相同的切分。绝不能用随机或带运行种子的切分（可以反复重摇到候选通过）
- 门槛：eligible ≥12 · control ≥5 · holdout ≥5；不达标输出 `NO_DECISION`
- ⚠️ 对同一个 dataset 再发流量**不会**改变切分（scenario_id 集合没变）。
  holdout 不够时唯一正当做法是**扩大 scenario 池**

产物在 `.omni/self-evolution/<run_id>/`（不入版本库 —— trace 可能含客户 payload）。

---

## 5. A/B replay 与门禁（Phase 5）

```bash
python scripts/replay.py --run <run_id> --set control  --repetitions 3
python scripts/gates.py  --run <run_id> --set control
# holdout 确认（只测一个候选）
python scripts/replay.py --run <run_id> --set holdout --repetitions 3 \
                         --variants "baseline,c2-restructured-procedure"
python scripts/gates.py  --run <run_id> --set holdout
```

**变体隔离**：每个候选一份完整的 `prompts.json`，靠 `OMNI_PROMPTS_OVERRIDE` 切换，
每次切换都**重启 dev server**（环境变量是进程级的，不重启还是上一个变体）。

⚠️ **候选的版本号必须让 `prompt_loader` 解析得出**：它从 `history[-1].versionId` 推导，
并在 `messages` 与该条不一致时追加 `-draft`。**顶层 `version` 键它不看。**
所以每个候选要追加一条 history，`versionId` = 候选 id、`messages` 与当前一致。
校验方式是导入 agent 自己的 `prompt_loader.get_prompt_version()`，**不要自己写平行逻辑**。

⚠️ **每次调用都要核对 trace 上的 `llm.prompt_template.version`**，不符即丢弃该格。
实测这条约束抓到了整整一个 arm 被错标（56 格）。

```bash
# 人工豁免某道门 —— 阈值一个字不改，只记录"谁以什么理由放行"
python scripts/gates.py --run <run_id> --set holdout \
  --waive-gate "延迟回退=业务上可接受：6s→6.6s 换取 CI 全正的质量提升"
# → verdict: WINNER_BY_HUMAN_WAIVER（与 WINNER 明确区分）
```

**长任务必须防睡眠**：`caffeinate -dimsu python scripts/replay.py …`。
它挡不住合盖睡眠 —— 睡眠会让 wall-clock 延迟读数完全失真
（实测出现单格 72 分钟，而整个 run 才跑了 40 分钟）。
门禁因此用**中位数**并剔除 >300s 的格子。

---

## 6. 排错对照表

| 症状 | 真实原因 | 处理 |
|---|---|---|
| `Cloud endpoint not configured` | 扩展宿主进程 env 里没有 `AWS_REGION`；proxy 不转发 env | 从终端带 env 重启 Kiro |
| `check_credentials` 长期 `ready:false / transient` | 后端 auth-check 抖动 | 本地工具不受影响；只有 `can_sign` 才是硬门 |
| 云端 SQL 返回 0 行 | region 错 / 时间两端没绑整数秒 / 漏 `"@telemetry_type"='traces'` / 自己的条件没加括号 / 用了不存在的 `service.name` | 默认当查错，不当没流量 |
| 本地 `list` 查不到刚产生的 trace | 窗口太宽 + 分页（store 跨 run 累积） | 窗口紧贴调用时刻 + 重试 |
| trace 上没有 `llm.prompt_template.version` | 没走 `opentelemetry-instrument` | 按 1.2 的启动命令 |
| 云上/本地一条 trace 都没有 | 打包时删了 `*.dist-info` | 保留它；用 entry point 数量做硬门 |
| evaluator 全给 `-1` | span 有 error（常见：region 错导致连不上 bedrock-runtime） | 看 replay 存下的响应信封 |
| 某个 evaluator 整列为空 | `create_evaluator` 用了该 level 不允许的占位符 | 见 1.4 |
| 某维度分辨率异常低 | 值域声明错（如把 `Helpfulness` 当 [0,1]，实际 [0,6]） | 用 `get_evaluator` 读真值 |
| dataset 条数比预期多 | `update_examples` 追加而非替换 | 先 invoke 再建 dataset |
| 门禁「0 个配对样本」 | 上游某处静默失败 | 从 arm 的 `打到分的 scenario 数` 往上查 |
