# Training：从 Agent 创建到通过评估持续改进

两套内容，同一套方法论：

| 目录 | 面向 | 实验在哪里做 | 是否涉及 Omni |
|---|---|---|---|
| [`cloudwatch/`](cloudwatch/) | 客户材料 | **云上**：AgentCore Runtime + CloudWatch GenAI Observability + AgentCore Evaluations，纯 AWS CLI | 只在最后一页一句话带过 |
| [`omni/`](omni/) | IDE 开发者 / 内部 | **本地**：CloudWatch Omni（Kiro 扩展）的 MCP 工具 | 是 |

每套都包含：

| 文件 | 内容 |
|---|---|
| `slides-outline.md` | 10 页 slides 大纲（评审后再生成 slides） |
| `workflow.md` | 全流程设计：每一步做什么、为什么、用什么工具、门禁怎么设、考量因素 |
| `skills/…/SKILL.md` | 交给 coding agent 执行的 SOP（每步停下等人确认） |
| `scripts/` | 可执行、可验证的脚本 |

## 验证状态（2026-10-20，us-west-2，account 613477150601）

### cloudwatch/scripts
| 脚本 | 验证方式 | 结果 |
|---|---|---|
| 00_prereq | 实跑 | ✅ |
| 01_deploy | `--synth-only` 实跑（baseline 模板与已部署版本逐字段一致；候选生成独立 runtime / role / stack） | ✅ 未实际部署候选 |
| 02_verify_observability | 实跑（1 次调用，13 个 span 落地） | ✅ |
| 03_dataset | 实跑（`cwtv1_control`，20 条，v1） | ✅ |
| 04_traffic | 实跑 2 个批次（40/40） | ✅ |
| 05_batch_eval | 实跑 2 次（20/20 session，8 个 evaluator） | ✅ |
| 06_insights | 实跑（40 session → 12 个根因） | ✅ |
| 07_recommend | 实跑（中文 v1 被 prompt 攻击防护误拦，英文底稿成功） | ✅ |
| 09_gates calibrate / register / compare | 实跑 A/A；compare 用 A/A 伪候选走通 | ✅ |
| 10_promote | 语法检查（线上变更，需人批准） | ⚠ 未实跑 |
| 99_cleanup | 只列出模式实跑 | ✅ 未删除 |

### omni/scripts
| 脚本 | 验证方式 | 结果 |
|---|---|---|
| run_step.sh 1（preflight） | 对在跑的 Omni MCP 实跑 | ✅ |
| omni_steps.py snapshot / ruler / candidate | 实跑（临时 run，已清理）；candidate 的禁止内容检查生效 | ✅ |
| gates_register.py | 用真实 run 的数据实跑 A/A；对已有候选分数的 run 正确拒绝 | ✅ |
| 3 / 4 / 5 / 6 / 8 / 9 / 10 / 11 | 底层脚本（se_*.py、replay.py）在 run `strands_agents-20261019T125024Z` 中全程实跑过 | ✅ |

## 本次为支持 training 对仓库做的改动
- `cdk/`：`AGENT_RUNTIME_SUFFIX` / `AGENT_TOOL_TIER` 环境变量 → 候选部署为独立 runtime + stack；未设置时 baseline 模板不变（已对比验证）
- `scripts/build_agent_bundle.sh`：`PROMPTS_FILE` 指定打进包的 prompts.json
- `scripts/se_gates.py`：`--registered` 使用事先登记的门禁；report_only evaluator 不进门禁
- `scripts/se_diagnose.py`：report_only evaluator 不计入失败
- `scripts/omni.mjs`：多 content 块解析 + MCP token 握手

## 云上遗留资源（验证产生，可用 `99_cleanup.sh` 清理）
- dataset `cwtv1_control-QZOofDBOSU`
- batch evaluation：`cwtv1_*` 3 个
- recommendation：`cwtv1_r1_rec_*`（不同尝试共 3 个）
- API 探测时建的 `cliprobe_*` 数据集 / batch evaluation / recommendation 已删除
