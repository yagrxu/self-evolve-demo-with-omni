# Phase 3 — 云上在线评估分数

- 流量 run：`traffic-dev_local-30130a4e`（dataset `dev_local`，endpoint `v1`）
- runtime：`selfevolve_demo_order_support-1ReSl92POU`  ·  region `us-west-2`
- 评估结果 log group：`/aws/bedrock-agentcore/evaluations/results/selfevolve_demo_online_eval-b8cUsWHGnR`
- 流量窗口：2026-09-18T07:00:14.543421+00:00 .. 2026-09-18T07:08:22.606583+00:00
- 生成时间：2026-09-18T07:33:08.120029+00:00
- 匹配到评分的 session：11 / 15

## 每个 evaluator 的云上均值（已归一化到 0–1，越高越好）

| Evaluator | 层级 | 覆盖场景 | 均值 | 及格率 |
|---|---|---:|---:|---:|
| `Builtin.Correctness` | TRACE | 11/15 | 0.864 | 73% |
| `Builtin.InstructionFollowing` | TRACE | 11/15 | 1.000 | 100% |
| `Builtin.Helpfulness` | TRACE | 11/15 | 0.146 | 0% |
| `Builtin.Faithfulness` | TRACE | 11/15 | 0.864 | 91% |
| `Builtin.ToolSelectionAccuracy` | TOOL_CALL | 11/15 | 1.000 | 100% |
| `Builtin.ToolParameterAccuracy` | TOOL_CALL | 11/15 | 1.000 | 100% |
| `Builtin.GoalSuccessRate` | SESSION | 11/15 | 0.727 | 73% |
| `PolicyGrounding` | TRACE | 11/15 | 0.909 | 91% |

> 未评分一律不计入均值，**绝不当 0 分** —— 把缺失当 0 会凭空造出「质量很差」的结论。

> ℹ️ 本 run 匹配到 **11 个不同 session**，所以 SESSION 级 evaluator 在云上是**有效**的 —— 与本地不同（决策点 R10：本地 dev server 的 collector `session.id` 只在启动时设一次，15 条调用共享一个 session，导致 n=1 伪装成 n=15）。云端每个请求带独立的 `runtimeSessionId`，因此这里不沿用本地的排除结论。

## 逐场景明细

| scenario | Correctness | InstructionFollowing | Helpfulness | Faithfulness | ToolSelectionAccuracy | ToolParameterAccuracy | GoalSuccessRate | PolicyGrounding |
|---|---|---|---|---|---|---|---|---|
| `dev_001` | 1.00 | 1.00 | 0.14 | 0.75 | 1.00 | 1.00 | 1.00 | 1.00 |
| `dev_002` | 0.50 | 1.00 | 0.11 | 0.25 | 1.00 | 1.00 | 0.00 | 1.00 |
| `dev_003` | 1.00 | 1.00 | 0.17 | 1.00 | 1.00 | 1.00 | 1.00 | 0.67 |
| `dev_004` | 1.00 | 1.00 | 0.17 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |
| `dev_005` | 0.50 | 1.00 | 0.06 | 0.75 | 1.00 | 1.00 | 0.00 | 1.00 |
| `dev_006` | 1.00 | 1.00 | 0.17 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `dev_007` | 1.00 | 1.00 | 0.17 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `dev_008` | 1.00 | 1.00 | 0.17 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `dev_013` | 1.00 | 1.00 | 0.17 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `dev_014` | 1.00 | 1.00 | 0.17 | 1.00 | 1.00 | 1.00 | 1.00 | 0.33 |
| `dev_015` | 0.50 | 1.00 | 0.14 | 0.75 | 1.00 | 1.00 | 1.00 | 1.00 |

## 产物

- `runs/traffic-dev_local-30130a4e/cloud-scores.json` —— 归一化后的逐场景分数
- `runs/traffic-dev_local-30130a4e/traffic.jsonl` —— 每条请求的输入/输出/echo

## 下一步

Phase 4：从这些云端 trace 里挑出差的，标注并导出到本地，作为 Phase 5 的合格样本集。
