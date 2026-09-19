# Phase 4 — 从云端导出差 trace，构建 Phase 5 样本集

- 来源流量 run：`traffic-prod_sim-0be7e8bc`, `traffic-dev_local-30130a4e`（dataset `dev_local`, `prod_sim`）
- 产物目录：`.omni/self-evolution/prod_sim-0be7e8bc+dev_local-30130a4e/`（不入版本库 —— trace 可能含客户 payload）
- 生成时间：2026-09-18T07:33:42.558656+00:00
- 对应 SKILL.md：**Step 3 ~ Step 6**
- `verify` 未参与（Phase 6 专用 held-out）：✅
- **判定：`READY_FOR_PHASE_5`**

## 样本集

| 集合 | 条数 | 门槛 | 结果 |
|---|---:|---:|---|
| eligible | 31 | ≥12 | ✅ |
| control | 25 | ≥5 | ✅ |
| holdout | 6 | ≥5 | ✅ |

> 切分是 `scenario_id` 的 **blake2b 稳定哈希**（holdout = 哈希落在最低 30%），是纯函数 —— 重跑得到完全相同的切分。
> 这不是洁癖：随机或带运行种子的切分可以反复重摇到候选通过，而最终报告里看不出任何异常（SKILL.md Invariant 5）。

holdout：`dev_004`, `dev_008`, `dev_015`, `prod_007`, `prod_009`, `prod_012`

## 失败模式（按影响条数排序）

| 失败模式 | evaluator | 影响条数 | 涉及原型 |
|---|---|---:|---|
| `low_Helpfulness` | `Builtin.Helpfulness` | 31 | apparel_expired, apparel_full_return, apparel_in_window, books_expired… |
| `low_Correctness` | `Builtin.Correctness` | 8 | apparel_expired, apparel_full_return, books_expired, coupon_multi_full… |
| `low_GoalSuccessRate` | `Builtin.GoalSuccessRate` | 7 | apparel_expired, apparel_full_return, books_expired, exchange_only… |
| `low_PolicyGrounding` | `PolicyGrounding` | 2 | multi_qty_partial |
| `low_ToolSelectionAccuracy` | `Builtin.ToolSelectionAccuracy` | 2 | home_exchange, multi_qty_partial |
| `low_Faithfulness` | `Builtin.Faithfulness` | 1 | exchange_only |
| `low_ToolParameterAccuracy` | `Builtin.ToolParameterAccuracy` | 1 | home_exchange |

**`low_Helpfulness` 的判词样例**（来自云端 evaluator，非我们撰写）：

> The user wants to know how much they can get back for returning their mechanical keyboard. The assistant retrieves the order and policy information, then provides a clear breakdown.
> 
> However, there's a critical error: the assistant incorrectly applies a 'restocking fee' (折旧费) of 10% as a deduction. Looking at the policy, the 10% is labeled as 'restocking_fee_pct' - this is a restocking fee, not a 

## 逐场景失败维度

| scenario | 原型 | 不及格的 evaluator |
|---|---|---|
| `dev_001` | refund_in_window | `Helpfulness` (0.14) |
| `dev_002` | exchange_only | `Correctness` (0.50), `Helpfulness` (0.11), `Faithfulness` (0.25), `GoalSuccessRate` (0.00) |
| `dev_003` | expired_warranty | `Helpfulness` (0.17) |
| `dev_004` | apparel_full_return | `Helpfulness` (0.17), `GoalSuccessRate` (0.00) |
| `dev_005` | apparel_expired | `Correctness` (0.50), `Helpfulness` (0.06), `GoalSuccessRate` (0.00) |
| `dev_006` | books_in_window | `Helpfulness` (0.17) |
| `dev_007` | home_in_window | `Helpfulness` (0.17) |
| `dev_008` | home_exchange | `Helpfulness` (0.17) |
| `dev_013` | damaged_out_of_window | `Helpfulness` (0.17) |
| `dev_014` | multi_qty_partial | `Helpfulness` (0.17), `PolicyGrounding` (0.33) |
| `dev_015` | coupon_multi_full | `Correctness` (0.50), `Helpfulness` (0.14) |
| `prod_001` | refund_in_window | `Helpfulness` (0.17) |
| `prod_002` | exchange_only | `Helpfulness` (0.14), `GoalSuccessRate` (0.00) |
| `prod_003` | expired_warranty | `Correctness` (0.50), `Helpfulness` (0.17) |
| `prod_004` | apparel_in_window | `Helpfulness` (0.17) |
| `prod_005` | apparel_expired | `Correctness` (0.50), `Helpfulness` (0.08), `GoalSuccessRate` (0.00) |
| `prod_006` | books_in_window | `Helpfulness` (0.17) |
| `prod_007` | home_in_window | `Helpfulness` (0.17) |
| `prod_008` | home_exchange | `Helpfulness` (0.17), `GoalSuccessRate` (0.00) |
| `prod_009` | perishable_no_return | `Helpfulness` (0.17) |
| `prod_010` | perishable_damaged | `Helpfulness` (0.17) |
| `prod_011` | coupon_single | `Helpfulness` (0.17) |
| `prod_012` | coupon_partial_multi | `Helpfulness` (0.17) |
| `prod_013` | damaged_out_of_window | `Helpfulness` (0.17) |
| `prod_014` | multi_qty_partial | `Helpfulness` (0.17), `ToolSelectionAccuracy` (0.50), `PolicyGrounding` (0.33) |
| `prod_015` | mixed_category_full | `Correctness` (0.50), `Helpfulness` (0.17) |
| `prod_016` | books_expired | `Correctness` (0.50), `Helpfulness` (0.14), `GoalSuccessRate` (0.00) |
| `prod_017` | home_exchange | `Helpfulness` (0.14), `ToolSelectionAccuracy` (0.50), `ToolParameterAccuracy` (0.50) |
| `prod_018` | coupon_partial_multi | `Helpfulness` (0.14) |
| `prod_019` | home_in_window | `Helpfulness` (0.17) |
| `prod_020` | apparel_full_return | `Correctness` (0.50), `Helpfulness` (0.11) |

## 下一步

Phase 5：按 SKILL.md Step 7 生成 prompt-only 候选，Step 8 在 control 集上做 paired replay，Step 9 过统计门禁，Step 10 在 holdout 上确认。

**候选必须针对上面排第一的失败模式**，且每个候选只改一件事 —— 一次改四处只能告诉你这一揽子有效，无法约减成最小安全变更。
