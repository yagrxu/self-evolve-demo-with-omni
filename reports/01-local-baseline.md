# Phase 2 — 本地基线报告

- run_id：`baseline-20260918T052034Z`
- 生成时间：2026-09-18T05:32:49.573483+00:00
- prompt 版本：`order_support-v1`（hash `c76023c4c730bed0`）
- 模型：`us.anthropic.claude-haiku-4-5-20251001-v1:0`
- 工具层 `AGENT_TOOL_TIER`：basic（Phase 5 需冻结的轴）
- dataset：`dev_local` v3（15 条，id `f2e82668-a329-4e55-94fd-74fcfa4d3dbf`）
- 基准日 `DEMO_AS_OF_DATE`：2026-09-17

## 执行情况

| 指标 | 值 |
|---|---|
| invoke 成功 | 15 / 15 |
| 拿到 trace | 15 / 15 |
| 延迟 中位数 / p95 | 5968 ms / 7842 ms |

## Evaluator 分数（已按 semantics.json 归一化到 0–1，越高越好）

| Evaluator | 层级 | 有效观测 | 均值 | 及格率 | 冻结值域 |
|---|---|---:|---:|---:|---|
| `Builtin.Correctness` | TRACE | 15/15 | 0.600 | 60% | [0.0,1.0] HIGHER_IS_BETTER |
| `Builtin.InstructionFollowing` | TRACE | 15/15 | 1.000 | 100% | [0.0,1.0] HIGHER_IS_BETTER |
| `Builtin.Helpfulness` | TRACE | 15/15 | 0.152 | 0% | [0.0,6.0] HIGHER_IS_BETTER |
| `Builtin.Faithfulness` | TRACE | 15/15 | 0.900 | 93% | [0.0,1.0] HIGHER_IS_BETTER |
| `Builtin.ToolSelectionAccuracy` | TOOL_CALL | 15/15 | 0.867 | 87% | [0.0,1.0] HIGHER_IS_BETTER |
| `Builtin.ToolParameterAccuracy` | TOOL_CALL | 15/15 | 1.000 | 100% | [0.0,1.0] HIGHER_IS_BETTER |
| `Builtin.GoalSuccessRate` ⚠️ | SESSION | 1/15 | 0.000 | — | [0.0,1.0] HIGHER_IS_BETTER |
| `PolicyGrounding` | TRACE | 15/15 | 0.933 | 93% | [0.0,3.0] HIGHER_IS_BETTER |

> **未评分（unscored）一律记 `None`，绝不当 0 分。** evaluator 缺失只降低 coverage —— 把缺失当 0 会凭空造出「变差了」的结论。

> ⚠️ **`Builtin.GoalSuccessRate` 不参与 per-example 聚合，也不进 Phase 5 门禁。**
> 本地 dev server 下 collector 的 `session.id` 只在启动时设一次（写在 `.env.omni`），所以全部 15 条调用共享同一个 session。SESSION 级 evaluator 因此只做了**一次**判断，再把同一个 score 和 explanation 复制给每个 itemKey —— 表面看是 15 个观测，实际 **n=1 且方差为 0**。当成独立观测会让 Phase 5 的 paired bootstrap 严重高估自由度。
> 上面的均值仅作为**单条 session 级观测**参考，及格率一栏因此留空。详见 `evaluators/semantics.json` 的 `_per_example_reason`。

## 确定性门

- `no_amount_when_ineligible`：5/5 通过

## 按原型看失败分布

| 原型 | scenario | PolicyGrounding | Correctness | 备注 |
|---|---|---:|---:|---|
| refund_in_window | `dev_001` | 1.00 | 1.00 |  |
| exchange_only | `dev_002` | 1.00 | 1.00 |  |
| expired_warranty | `dev_003` | 0.67 | 0.00 |  |
| apparel_full_return | `dev_004` | 1.00 | 0.00 |  |
| apparel_expired | `dev_005` | 1.00 | 1.00 |  |
| books_in_window | `dev_006` | 1.00 | 1.00 |  |
| home_in_window | `dev_007` | 1.00 | 1.00 |  |
| home_exchange | `dev_008` | 1.00 | 0.00 |  |
| perishable_no_return | `dev_009` | 1.00 | 1.00 |  |
| perishable_damaged | `dev_010` | 1.00 | 1.00 |  |
| coupon_single | `dev_011` | 1.00 | 1.00 |  |
| coupon_partial_multi | `dev_012` | 1.00 | 1.00 |  |
| damaged_out_of_window | `dev_013` | 1.00 | 0.00 |  |
| multi_qty_partial | `dev_014` | 0.33 | 0.00 |  |
| coupon_multi_full | `dev_015` | 1.00 | 0.00 |  |

## 产物

- `runs/baseline-20260918T052034Z/manifest.json` —— 冻结的实验配置（evaluator 语义、dataset 版本、prompt hash）
- `runs/baseline-20260918T052034Z/invocations.jsonl` —— 每条调用的输入/输出/trace id
- `runs/baseline-20260918T052034Z/evaluation-raw.json` —— evaluator 原始返回

## 下一步

Phase 3：`scripts/build_agent_bundle.sh` → `cd cdk && npm run deploy` → `python scripts/traffic.py --dataset prod_sim`。
