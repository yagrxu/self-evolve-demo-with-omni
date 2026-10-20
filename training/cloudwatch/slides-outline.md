# Slides 大纲（10 页）—— 用 CloudWatch GenAI Observability 让 Agent 持续变好

> 已生成 HTML 版（re:Invent 模板，15 页，含 CloudWatch 与 Omni 两版调用）：`training/slides/agentcore-eval-loop/index.html`。
> 对外讲解统一用六步：① 创建 Agent 并开启可观测性（Transaction Search 含 `logs put-resource-policy`）② CDK 部署 + 配置评估 ③ 建数据集并按用途分组 ④ 跑基线并测出噪声（多批次 + A/A + 登记门禁）⑤ 找关键问题、只改一层 ⑥ 分阶段确认再上线（对照同包重建）。
> 以下为最初的大纲，保留备查。核心是 **CloudWatch GenAI Observability + AgentCore Evaluations**；Omni 只在第 10 页一句话带过（客户材料不含 Omni 实验）。
> 每页：标题 · 要讲的 3–4 点 · 建议的图 / 截图 · 演讲提示。数据全部来自本仓库实测（us-west-2，2026-10-20）。

---

### 1. 为什么"上线"只是开始
- Agent 的质量问题大多是**不报错**的：答错金额、前后矛盾、该拒绝时暗示有例外
- 没有可观测性 → 不知道错在哪；没有评估 → 不知道改了有没有用；没有门禁 → 不知道改了会不会更糟
- 本次的目标：把「感觉变好了」变成「有证据地变好了」
- 图：一条坏回答的截图（ORD-10008 超窗口被答成"可以退货"）
- 提示：先用一个客户能共鸣的真实错误开场

### 2. 闭环全景：从 Agent 创建到持续改进
- 准备：CDK 部署 → 自动埋点 → 验证 trace
- 基线：数据集 → 带标准答案的批量评估 → 失败分类
- 改进：只改一层的候选 → 配对比较 → 分阶段门禁 → 上线 / 回滚
- 全程只用 AWS CLI，每一步可交给 coding agent 执行、人来确认
- 图：workflow.md 第 0 节的流程图

### 3. 第一步：部署即可观测
- AgentCore Runtime Direct Code Deploy：`opentelemetry-instrument` 作 entrypoint，ADOT 自动埋点，不改业务代码
- Transaction Search 把 OTel span 写进 CloudWatch Logs `aws/spans`
- 验证三类 span：`invoke_agent` · `execute_tool` · `chat`（一条请求 13 个 span）
- 截图：CloudWatch → GenAI Observability → Bedrock AgentCore → Agents → 某个 session 的 trace 树
- 提示：强调「没开 Transaction Search 不会报错，只是后面所有评估都是 0 条」

### 4. 在 GenAI Observability 里读懂一条 trace
- 会话 / trace / span 三层：用户输入、每次模型调用、每次工具调用的参数与返回
- token、延迟按 span 分解：成本在哪里花掉的
- 在线评估（Online Evaluation）结果和 trace 关联：哪条 session 哪个维度低分
- 截图：一条 trace 的工具返回 vs 模型最终回答（工具返回正确、回答算错）
- 提示：让客户意识到「读工具返回」是判断该改哪一层的关键

### 5. 第一波数据集：标准答案从哪里来
- AgentCore Dataset（`create-dataset` + `create-dataset-version`）：scenario_id、输入、标准答案、断言、期望工具轨迹
- 标准答案必须来自确定性规则或人工确认，**绝不**复制线上回答
- 至少两份不相交的集合：control（诊断、挑候选）与 confirm（从未用于挑选）
- 图：一条数据集样例（字段标注）
- 提示：数据集的规模由「要分辨多大的差异」决定（见第 8 页）

### 6. 评估：在线评估 + 带标准答案的批量评估
- 在线评估：线上流量持续打分，覆盖全部 session（本 demo 35/35）
- 批量评估（`start-batch-evaluation`）：按 session 注入 ground truth，逐 session 结果写入专用 log group
- 判官要校准：读 `get-evaluator` 的真实值域；新判官做误报测试（本 demo 的一个安全判官对正常回复系统性误报）
- 截图：batch evaluation 的 evaluatorSummaries + 一条逐 session 判词
- 提示：评估本身也是测量仪器，需要被验证

### 7. 分类：失败在哪一层
- Insights（`Builtin.Insight.FailureAnalysis / UserIntent / ExecutionSummary`）：失败聚类 → 根因 → 受影响 session → 修复建议（fixType）
- 六个可改的层：Policy · Tool · Workflow · Skill · Prompt · Model —— 从上游往下查，改证据指向的最低一层
- 普遍性：同一 scenario 在多个独立批次里失败几次（k/k 系统性、1/k 偏波动）
- 实测提醒：insight 把计算类错误也标成 `SYSTEM_PROMPT_FIX`，人工复核后其实应改工具
- 图：根因 × 修复层 × 普遍性表（06 的输出）

### 8. 门禁：不能写死，要先校准、事先登记
- A/A 实测：两个**完全相同**的 arm，质量 Δ 的 95% CI 是 [−0.13, +0.10]，token 差 +10%，延迟差 +6%
- 所以「CI 下界 ≥ −0.05、token ≤ 10%」这种固定门槛，会拒掉一个什么都没改的候选
- 门槛 = 噪声（A/A）+ 样本量（单格翻转分辨率）+ 业务预算；在任何候选结果出现之前登记
- 样本量反推：分辨 ±0.05 约需 37 个 scenario，±0.03 约需 100 个
- 图：A/A 结果 + 登记文件截图（含「可达性」警告）

### 9. 分阶段收紧：S0 → S1 → S2 → S3
- S0 本地 control：非劣效筛选；S1 confirm 集：没见过的题上不更差
- S2 云上灰度：真实流量证明「真的更好」；S3 全量：持续监控 + 自动回滚
- 流量越大、影响越广，门禁越严 —— 样本多了 CI 自然变窄，门禁才能收紧而不变成永远过不了
- 上线需要人确认，并保留一键回滚
- 图：四阶段阶梯图（样本量 / 影响范围 / 门禁严格度）

### 10. 落地清单 & 下一步
- 今天就能做：开 Transaction Search → 部署时带 ADOT → 建第一份数据集 → 跑 2 个 baseline 批次做 A/A
- 交给 coding agent：`skills/agentcore-eval-loop/SKILL.md` + `scripts/00–10`，每步停下给人确认
- 常见坑：`aws/spans` 必须在 log group 列表里；insight 与 evaluator 不能同批；推荐只接受 1 个 evaluator
- 如果团队在 IDE 里开发，CloudWatch Omni（IDE 扩展）提供同一套 trace / 评估的本地视图（一句话带过，不展开）
- 图：一页 checklist
