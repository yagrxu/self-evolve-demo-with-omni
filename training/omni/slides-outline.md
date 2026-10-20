# Slides 大纲（10 页）—— 在 IDE 里用 CloudWatch Omni 做有证据的 Agent 改进

> 只写大纲，评审后再生成。与 cloudwatch 版是同一套方法论；这一版的实验在本地 IDE 里完成，数据来自本仓库一次真实运行。

---

### 1. 一次真实的演进，结论先行
- 出发点：一个"故意做弱"的售后 agent（prompt 只有 42 个字）
- 结果：在从未见过的 20 条题上，正确合计金额 19/39 → 29/39；代价 token +30%
- 但按 SKILL 原有的固定门禁，结论是 NO_CHANGE —— 这次演讲讲的就是这两句话之间发生了什么
- 图：fresh 集上 baseline vs c1 的金额核对对比

### 2. 闭环：生产 → 本地 → 生产
- 生产：AgentCore 部署 + 在线评估，持续打分
- 本地（Omni）：拉生产 trace → 复现 → 诊断 → 候选 → paired replay → 门禁
- 回到生产：S2 云上灰度再证明一次
- 每步停下由人确认（演示时每一步都截图）
- 图：11 步流程图（标出停点）

### 3. Omni 是什么，在这个闭环里做什么
- IDE 扩展 + MCP 工具：本地 dev server、本地 trace、评估、数据集、云端 trace 查询
- 让 coding agent 直接调用：`search_agent_traces`、`manage_evaluations`、`manage_test_agent`……
- 前提三件事：prompt 交给 `prompts.json` 管理、`opentelemetry-instrument` 启动、span 带 prompt 版本 / hash
- 截图：Omni 面板 + 一条带 `llm.prompt_template.version` 的本地 trace

### 4. 快照，而不是"锁定只改 prompt"
- 基线 = 所有层的状态指纹：policy / tool / workflow / skill / prompt / model
- 改哪一层，要看完 trace 才决定
- 一轮只改一层，其余层字节一致 —— 测出的差值才能归因
- 图：manifest.json 的 layers 段

### 5. 先检查尺子：生产评估结果的体检
- 在线评估 35/35 覆盖，但旧配置里 Helpfulness 值域写错、安全判官方向写反、生产根本没有 safety evaluator
- 本地 replay 的尺子要和生产对齐，并逐个用 `get_evaluator` 核对
- 一个安全判官对正常客服回复系统性误报 → 降为只报告
- 图：check.json 表格

### 6. 诊断：重复采样 + 按层归因
- 生产每个 scenario 只有 1 个样本 → 不变的 baseline 本地跑 3 遍
- 32 个失败格：Prompt 14 · Tool 12 · Workflow 6；系统性失败 5 个
- 证据来自 trace 里的工具返回：数据对、结论错 → Prompt；模型心算出错 → Tool；先开退货单再查政策 → Workflow
- 截图：dev_014 的 trace（只有 get_order → create_return_label）

### 7. 噪声比你想象的大
- 判官重评一次，候选 Δ 从 +0.023 变成 +0.050
- 同一条题：一次 3/3 算错，换个时段 3/3 算对
- A/A：同一个 prompt 两组之间 Δ 的 CI 宽 ±0.045
- 结论：每格多评几次、重复分批独立跑、样本量按要分辨的差异反推
- 图：两次 holdout 的逐 scenario 对比

### 8. 门禁要动态、要事先登记
- 固定阈值在样本少时数学上不可达（7 条 holdout，单格翻转就是 −0.048）
- 门槛 = A/A 噪声 + 分辨率 + 业务预算；在任何候选结果出现前登记
- 看到结果后的调整：允许，但必须记录时间、理由，标记 `calibrated_after_results`
- 图：gates.registered.S0.json + decision.json 里的校准记录

### 9. 分阶段收紧 + 跨层副作用
- S0 control → S1 fresh → S2 云上灰度 → S3 全量，样本越多门禁越严
- 本次 c1 只通过了 S0/S1 的非劣效检验，"真的更好"留给 S2
- 跨层副作用：要求"写出金额"把 Tool 层的心算错误暴露给用户 → 下一轮改 Tool 层
- 图：四阶段阶梯 + 下一轮计划

### 10. 落地：交给 coding agent 的那份 SOP
- `run_step.sh 1…11`：一次一步，跑完即停
- skill：omni-self-evolution v2（18 条实测修订）+ change-surface
- 生产侧不用 Omni 也能做同样的事：AWS CLI 版本见 cloudwatch 材料
- 图：checklist
