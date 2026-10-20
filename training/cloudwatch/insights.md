# AgentCore Insights：它能做什么、给到什么程度、我们怎么用它做决定

> 数据来自 run `cwdemo1`：baseline 在 control 集上跑 4 个批次（80 个 session）后做的 insight 分析
> `cwdemo1_baseline_control_insight_173719-d55ba2adc0`（控制台截图见 slides 第 12 页 ⑥）。
> 文档：<https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/batch-evaluations-insights.html>

---

## 1. 它是什么

Insights 是 **Batch Evaluation 的一种运行方式**：不给每个 session 打分，而是把一批 session 的完整 trace 读完，**聚类、归因、总结**。

```bash
aws bedrock-agentcore start-batch-evaluation --cli-input-json '{
  "batchEvaluationName": "cwdemo1_baseline_control_insight",
  "insights": [{"insightId": "Builtin.Insight.FailureAnalysis"},
               {"insightId": "Builtin.Insight.UserIntent"},
               {"insightId": "Builtin.Insight.ExecutionSummary"}],
  "dataSourceConfig": {"cloudWatchLogs": {"serviceNames": ["selfevolve_demo_order_support.v1"],
                       "logGroupNames": ["aws/spans", "<runtime log group>"],
                       "filterConfig": {"sessionIds": [ …80 个… ], "timeRange": {…}}}}}'
```

实测约束：
- insight id 只有这三个：`Builtin.Insight.FailureAnalysis / UserIntent / ExecutionSummary`（写成 `Builtin.FailureAnalysis` 会被接受，但所有 session 都失败）
- `insights` 和 `evaluators` 不能放在同一个 batch evaluation 里
- 和评分一样，`logGroupNames` 必须包含 `aws/spans`

---

## 2. 三个视角（控制台的三个页签）

| 页签 | 回答的问题 | 本次结果（80 个 session） | 用途 |
|---|---|---|---|
| **Failure analysis** | 哪里失败、为什么 | 2 大类：政策理解与计算错误 **20/80**；工具参数与执行失败 **9/80** | 定优先级、找证据 |
| **User intents** | 用户在问什么 | 15 类：质量问题退货 8 · 退货问运费 8 · 部分退货 4 · … | 检查数据集是否覆盖真实意图分布；失败是否集中在某类意图 |
| **Execution summaries** | agent 实际怎么处理的 | 12 类处理路径 | 看「常见路径」长什么样，对照失败 session 走的是不是另一条路 |

### Execution summary 读法（以你贴的这一条为例）

> **Refund Eligibility Assessment and Amount Calculation（16 个 session）**
> This cluster handles customer refund requests by retrieving order details and applicable refund policies, then calculating precise refund amounts based on product category-specific rules including depreciation fees, restocking charges, and shipping cost responsibilities. The agents systematically compare order timelines against return windows, apply policy-specific fee structures (such as the DAMAGED_OVERRIDE rule for damaged goods), and provide customers with comprehensive refund breakdowns and next steps for generating return labels or RMA numbers.

它描述的是**一条处理路径**，不是好坏判断：`get_order → get_refund_policy → 比较天数与窗口 → 按品类规则算费用 → 给出明细与下一步`。
读出来的信息：
1. **主流路径是「模型自己算」**：16 个 session 都是取原始数据后由模型计算折旧费、运费、窗口 —— 这正是 Tool 层问题的温床（见 §5）。
2. **「next steps for generating return labels」**：这条路径的结尾会主动引导开退货单 —— 与 Failure analysis 里「未核验资格就调用 create_return_label」可以对上。
3. 每个 session 还有 `approachTaken` / `finalOutcome`（例如 prod_007：取订单 → 取家电政策 → 算出 ¥379.05），可以用来抽查「成功」是不是真成功。

---

## 3. Failure analysis 的层级：从大类一直到 span

```
失败大类   Policy Misinterpretation and Calculation Errors ··················· 20/80
 ├ 子类    Policy Misinterpretation and Logic Contradiction ·················· 12
 │  ├ 根因 Return Eligibility Policy Verification Before Tool Invocation ····· 8
 │  └ 根因 Policy Window Confusion: Return vs. Exchange Eligibility ·········· 4
 ├ 子类    Coupon Proration and Refund Calculation Errors ···················· 5
 │  ├ 根因 Refund Calculation Procedure Incompleteness ······················· 2
 │  └ 根因 Coupon Proration Arithmetic Precision and Rounding Rule Violations · 3
 └ 子类    Incorrect Return Type Classification → Shipping Refund Errors ····· 3
失败大类   Incomplete Tool Parameters and Execution Failures ················· 9/80
 └ 根因    Eligibility Verification Logic Gaps 3 · Partial Return Quantity Gap 3 · Conflicting Data Sources 2 · …
```

每个根因下都列出受影响的 session，**每个 session 定位到具体 span**：

| 字段 | 示例（`cwt-cwdemo1-baseline-prod_005-b1-0`） |
|---|---|
| explanation | 在 span `a29f33bc…`，agent 已说明订单 40 天、超出 30 天窗口，却又说「我们仍可为您处理」并调用 `create_return_label` |
| signals.category | `hallucination-category-hall-misunderstand`（失败分类体系，共 30+ 类） |
| signals.evidence | 引用原话 + 政策字段 `exchange_after_window=false` |
| signals.confidence | 0.9 |
| fixType | `SYSTEM_PROMPT_FIX` |
| recommendation | 一段方向性建议 |

这一层是 insights 最有价值的地方：**从「Correctness 低」直接跳到「哪条 trace 的哪个 span、为什么」**，人工核对只需点进去看。

---

## 4. 它给「怎么改」到什么程度

| 输出 | 内容 | 能直接用吗 |
|---|---|---|
| `fixType` | 改哪一层：`SYSTEM_PROMPT_FIX` / `TOOL_DESCRIPTION_FIX` / … | **只作参考**：本次 27 个根因有 26 个是 `SYSTEM_PROMPT_FIX`，包括「券分摊算错」 |
| `recommendation` | 方向性文字，例如「在 system prompt 中加入券分摊的分步计算流程，至少保留 4 位小数……」 | **不能直接用**：说了改什么，没给改好的内容 |
| Create recommendation（另一个 API：`start-recommendation`） | 一段**改写好的** system prompt 或工具描述 | 可作为候选，必须对照政策审核（验证时它漏掉了破损商品退原始运费的例外） |

**结论**：insights 回答「先修什么、证据在哪」；「改成什么样」要么用 Recommendation 生成草稿，要么人写；「改哪一层」由人按证据判断。

两个局限：
- **有随机性**：同样 80 个 session 跑两次，第一次聚成 12 个平铺根因，第二次是 2 大类、多个子类。看大类排序，不要把细的条数当精确值。
- **不知道标准答案**：它判断「看起来失败」；是否真错，以带标准答案的 batch evaluation（Correctness）为准。

---

## 5. 我们怎么用它做决定：为什么先改 Prompt、再改 Tool

### 第 1 轮：照 insights 的方向改 Prompt（p1）
- insights 最大的子类是「逻辑矛盾 / 资格判断」（12 个 session），fixType 全是 `SYSTEM_PROMPT_FIX`
- 对照 4 个批次的普遍性：系统性失败（≥3/4）的 prod_005 / prod_008 正是「超窗口仍暗示可退、前后矛盾」，prod_013 / prod_020 是「结论对但不写金额」—— 工具数据是对的，模型判断 / 表达出错 → **Prompt 层**
- p1：在 v1 后追加两句（只给一个结论、超窗口不暗示例外；可退款时写出逐件金额和合计）

结果（S0，control 80 格）：
| | baseline → p1 |
|---|---|
| 目标场景 | prod_008 3/4→**0/4** · prod_014 3/4→**0/4** · prod_005 3→1 · prod_013 3→1 |
| 新出现的失败 | prod_001 0→**3** · prod_018 1→**3** · prod_010 0→**2**（全是金额计算类） |
| Correctness 失败总数 | 23 → **23** |
| Helpfulness 失败 | 3 → 13 |
| 门禁 | **FAIL**（均值 −0.010） |

**为什么失败**：prompt 要求「把金额写清楚」，模型就逐项心算券分摊、折旧费基数、运费 —— insights 里「Coupon Proration … Rounding Rule Violations」「Refund Calculation Procedure Incompleteness」这些根因被放大了。改 prompt 只能让模型「更努力地算」，算错的概率还在。

### 第 2 轮：同一批根因，换一层修 —— Tool（t1）
- 判断依据（不是 fixType）：失败发生在「模型自己做确定性计算」的步骤 —— 日期窗口比较、券分摊、折旧费基数、运费规则。这些可以写成代码，**放进工具里就不再是概率问题**
- Execution summary 也印证：主流路径（16 个 session）就是「取原始数据 → 模型计算」
- t1：prompt 保持 v1，只打开 `AGENT_TOOL_TIER=enhanced`（`check_return_eligibility` / `calculate_refund` / `get_coupon_allocation` 直接返回资格与金额）

结果：
| | 对照 → t1 |
|---|---|
| Correctness 失败（control 80 格） | 23 → **13** |
| 系统性失败场景 | prod_005 / prod_008 / prod_014 → 全部 0/4 |
| S1（40 道未用于挑选的题，对照 b0） | 质量 Δ **+0.162**，95% CI **[+0.083, +0.243]** |
| 代价 | token +56% |

### 这次学到的用法
1. **insights 定「修什么」**：按大类 / 子类的受影响 session 排优先级
2. **普遍性定「真不真」**：用多批次的 k/批次数确认是系统性失败，而不是一次采样的波动
3. **trace 定「改哪层」**：点进 insights 给的 span，看工具返回是否正确、是谁在算 —— 不照抄 fixType
4. **门禁定「改没改好」**：任何候选都要过事先登记的门禁；p1 就是在门禁上暴露了副作用
