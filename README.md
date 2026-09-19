# AgentCore Self-Evolution Demo（CloudWatch Omni）

一个端到端可复现的 Demo：**agent 从"效果不好"出发 → 云上跑真实流量 → 导出差 trace →
本地 A/B 调优 → 重新部署 → held-out 数据集验证提升**。

场景是电商订单售后客服。部署走 CDK + AgentCore **Direct Code Deploy**（S3 zip，无需 Docker）。
Region 固定 **us-west-2**（与 Omni Space 同 region —— 云端 trace 查询走它），AWS profile **default**。

---

## 文档导航

| 文档 | 内容 |
|---|---|
| **[.claude/skills/omni/self-evolution/SKILL.md](.claude/skills/omni/self-evolution/SKILL.md)** | **主产出**：`omni-self-evolution` 方法论 SOP（11 步 + 6 条硬不变量 + 6 道统计门）。同一份也放在 `.kiro/skills/omni-self-evolution/` |
| **[docs/STORYLINE.md](docs/STORYLINE.md)** | 演示故事线（24 张 slide）：技术背景、循环轮廓、Omni 16 工具全景、循环详解、human-in/on-the-loop |
| **[docs/RUNBOOK.md](docs/RUNBOOK.md)** | 调用流程手册：只记录跑通的调用 + 每个参数必须这样做的理由 |
| **[docs/BUILD-LOG.md](docs/BUILD-LOG.md)** | **施工日志**：每一步的命令、原始输出、决策；20+ 个实测踩坑（约一半是不报错的静默失败） |
| **[docs/PLAN.md](docs/PLAN.md)** | 设计与计划：MCP/Skill/AWS 验证结论、场景设计、6 个 Phase、风险表 |

**想直接看方法论，读 SKILL.md；想讲给别人听，读 STORYLINE；想复现，读 RUNBOOK；想知道"为什么这样做"，读 BUILD-LOG。**

> **本次运行结局：`NO_CHANGE`。** 4 个 prompt 候选、486 格 paired replay，没有一个确立对 baseline
> 的可泛化提升 —— 其中 c2 在 control 上看似胜出（质量 +0.0348、CI 全正），被 held-out 揭穿为过拟合
> （holdout 上质量塌回 ~0、PolicyGrounding 回退 0.13）。**能拒绝一个假的优化，正是这套方法论的价值所在。**

---

## 六个 Phase 与对应命令

| Phase | 做什么 | 命令 | 产物 |
|---|---|---|---|
| 1 | v1 agent（故意做差）+ 本地埋点 | `make datasets` · `make serve` | `datasets/*.json` |
| 2 | 本地基线：dataset + 9 个 evaluator | `make baseline` | `reports/01-local-baseline.md` |
| 3 | CDK 部署 + 云上流量模拟 | `make deploy` · `make traffic-prod` | `reports/02-cloud-baseline.md` |
| 4 | 从云上导出差 trace 到本地 | `make export-bad` | `.omni/self-evolution/<run>/` |
| 5 | 三条优化轴 + 本地 paired A/B | `make ab` | `reports/03-local-ab.md` + `decision.json` |
| 6 | 重新部署 + held-out 验证 | `make deploy-v2` · `make traffic-verify` | `reports/04-cloud-verify.md` |

---

## 前置条件

### 1. Omni MCP 必须为**本目录**运行 ⚠️

Omni 的 MCP server 跑在 Kiro 扩展进程内，**一次只服务一个 workspace**
（实测证据见 BUILD-LOG 决策点 R1）。所以要先**在 Kiro 里打开本项目目录**：

```
/Users/yagrxu/me/collaborations/2026/tfc/self-evolve-demo-with-omni
```

自检：

```bash
node scripts/omni.mjs call check_credentials '{}'
# 期望 {"can_sign": true, "ready": true, ...}
# 报 "找不到 .omni/mcp-port" → Kiro 还没为本目录启动 Omni
```

### 2. Python 环境

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r agent/requirements.txt
```

### 3. AWS

`default` profile 即可。已确认就绪、**无需再操作**的前置项：

- `aws xray get-trace-segment-destination --region us-west-2` → `CloudWatchLogs / ACTIVE`
- X-Ray indexing rule → 100% 采样

---

## 三条优化轴都由外部配置驱动（代码零改动）

这是 Phase 5 能成立的结构性前提 —— skill 要求"除被测轴之外 model/tools/code/dataset 全部冻结"：

| 轴 | 开关 | Phase 5 candidate |
|---|---|---|
| prompt | `OMNI_PROMPTS_OVERRIDE=<另一份 prompts.json>` | `c1_prompt_v2` |
| tools | `AGENT_TOOL_TIER=enhanced` | `c2_tool_tier_enhanced` |
| model | 改 `prompts.json` 的 `model.modelId`（经 `get_model_config()` 真实生效） | `c3_model_swap` |

---

## 关键设计取舍（都在 BUILD-LOG 有完整记录）

- **"今天"是固定值** `DEMO_AS_OF_DATE=2026-09-17`。用 `date.today()` 会让同一条
  dataset 明天跑出另一个答案，baseline 与 candidate 就不可比 —— A/B 结论直接失效。
- **oracle 由 `agent/tools.py` 的纯函数渲染**，和 agent 运行时是同一份算法。
  手写标注答案一旦算错（券分摊最容易），evaluator 会把正确回答判成错的 —— 那是最恶劣的评估污染。
- **三份 dataset 的订单区间互不相交**，脚本里有断言强制校验。`verify` 是 Phase 6 的
  held-out 集，保证提升不是"记住了答案"。
- **工具分 BASIC / ENHANCED 两层。** 第一版工具做得太强，实测发现 v1 把所有场景都答对了，
  Demo 前提不成立；重设计后 BASIC 层只给原始数据，计算类工具成为「加 tool」这条优化轴的实际内容。
- **未评分一律记 `None`，绝不当 0 分。** evaluator 缺失只降低 coverage —— 当成 0 会凭空
  造出"变差了"的结论。

---

## 目录结构

```
agent/          Strands + BedrockAgentCoreApp；prompts.json 由 Omni 托管
  fixtures/     确定性 JSON 后端（订单/政策/FAQ）—— 无外部网络，replay 隔离可证明
datasets/       三份数据集，oracle 由 tools.py 纯函数生成
evaluators/     PolicyGrounding rubric + 打分语义冻结声明
cdk/            AgentCore Runtime（Direct Code Deploy）+ 自定义 evaluator + 线上持续评估
scripts/        omni.mjs（MCP 桥）· 各 Phase 的执行脚本
reports/        01~04 各 Phase 的报告
docs/           PLAN.md（设计）· BUILD-LOG.md（施工日志）
```
