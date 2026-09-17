/**
 * 全局常量。集中在一处，避免"某个文件忘了写 region"这类静默错误。
 */

/**
 * 显式写死 us-east-1。
 *
 * 理由（见 docs/BUILD-LOG.md 决策点 R4）：本机 AWS profile 的默认 region 是
 * ap-southeast-1，但 AgentCore runtime、X-Ray→CloudWatchLogs 投递、以及 Omni
 * 云端 trace store 全在 us-east-1。如果依赖 profile 默认值，会静默部署/查询到
 * 一个空 region —— trace 一条都查不到，而且极难 debug。
 */
export const REGION = 'us-east-1';

/**
 * 所有资源统一前缀。
 *
 * 理由（决策点 R5）：账号 613477150601 里已经有别人的 AgentCore runtime
 * （cat_demo_strands / cat_demo_langgraph）。加前缀 + 独立 stack 保证本 Demo
 * 只新建、绝不误改误删已有资源。
 */
export const PREFIX = 'selfevolve-demo';

/**
 * AgentCore runtime 名的约束是 `[a-zA-Z][a-zA-Z0-9_]{0,47}` —— 不能有连字符，
 * 所以这里用下划线版本，不能直接套用 PREFIX。
 */
export const RUNTIME_NAME = 'selfevolve_demo_order_support';

/** RuntimeEndpoint 名。Phase 3 用 v1，Phase 6 用 v2（并存，便于并排对比）。 */
export const ENDPOINT_NAME = process.env.AGENT_ENDPOINT_NAME ?? 'v1';

/** 管理运行时的托管 Python 版本。必须与 scripts/build_agent_bundle.sh 里 pip 的 --python-version 一致。 */
export const PYTHON_RUNTIME = 'PYTHON_3_12';

/**
 * entryPoint 上限 2 个元素（CFN schema: EntryPoints minItems 1 maxItems 2）。
 * 前缀 `opentelemetry-instrument` 就是 ADOT 自动埋点的加载方式 ——
 * 不用改一行业务代码、也不用 Docker，trace 直接进 CloudWatch。
 * Amazon 内部已有生产服务用的就是这个组合（见 BUILD-LOG Phase -1.10）。
 */
export const ENTRYPOINT = ['opentelemetry-instrument', 'agent.py'];

/** baseline 模型。也写进 prompts.json；这里的 env 只是给 runtime 一个可观测的回显值。 */
export const BASELINE_MODEL_ID = 'us.anthropic.claude-haiku-4-5-20251001-v1:0';

/**
 * Phase 5 的三条优化轴都靠 runtime 环境变量切换，代码零改动：
 *   AGENT_PROMPT_VARIANT  仅作标记，写进 trace 便于归因
 *   ENABLE_COUPON_TOOL    candidate c2（加 tool）的开关
 * prompt 与 model 本身由打进包里的 prompts.json 决定。
 */
export const PROMPT_VARIANT = process.env.AGENT_PROMPT_VARIANT ?? 'v1-baseline';
export const ENABLE_COUPON_TOOL = process.env.ENABLE_COUPON_TOOL ?? 'false';
