import * as fs from 'fs';
import * as path from 'path';
import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import { Construct } from 'constructs';

const REPO_ROOT = path.resolve(__dirname, '..', '..');

/** evaluators/policy_grounding.json 的形状（只声明我们真正用到的字段）。 */
interface PolicyGroundingRubric {
  name: string;
  level: string;
  description: string;
  model_id: string;
  instructions: string;
  rating_scale: { numerical: Array<{ value: number; label: string; definition: string }> };
}

export interface EvalStackProps extends StackProps {
  readonly runtime: agentcore.IBedrockAgentRuntime;
  readonly endpoint: agentcore.IRuntimeEndpoint;
}

/**
 * 云端评估：自定义 evaluator + 对线上流量的持续评估。
 *
 * 为什么把 evaluator 也写进 CDK
 * ------------------------------
 * `PolicyGrounding` 的 rubric 在两个地方被用到：
 *   · Phase 2 本地基线 —— 通过 `manage_evaluations(create_evaluator)` 创建
 *   · Phase 3/6 云上   —— 就是这个 stack
 * 两边**必须是同一份 rubric**，否则"本地 A/B 选出的 winner"和"云上验证的分数"
 * 根本不在同一把尺子上，整条 self-evolution 链路的结论就断了。
 *
 * 所以这里直接读 `evaluators/policy_grounding.json` —— 让文件成为唯一真相来源，
 * 而不是在 TS 里再抄一遍 prompt（抄一遍就一定会漂移）。
 *
 * OnlineEvaluationConfig 做什么
 * -----------------------------
 * 它让云上的评分**自动产生**：runtime 每处理一条真实流量，配置里的 evaluator 就
 * 对那条 trace 打分，结果作为 evaluation span 落回 CloudWatch。
 * Phase 3/6 只要 `manage_evaluations(action="results", dataSource="cloud")` 读就行，
 * 不需要手工触发 —— 这才叫"模拟真实用户流量 + 自动打分"。
 */
export class EvalStack extends Stack {
  constructor(scope: Construct, id: string, props: EvalStackProps) {
    super(scope, id, props);

    const rubric: PolicyGroundingRubric = JSON.parse(
      fs.readFileSync(path.join(REPO_ROOT, 'evaluators', 'policy_grounding.json'), 'utf8'),
    );

    // ── 自定义 evaluator：PolicyGrounding ───────────────────────────────
    const policyGrounding = new agentcore.Evaluator(this, 'PolicyGrounding', {
      evaluatorName: `selfevolve_demo_${rubric.name}`,
      description: rubric.description,
      level: agentcore.EvaluationLevel.TRACE,
      evaluatorConfig: agentcore.EvaluatorConfig.llmAsAJudge({
        instructions: rubric.instructions,
        modelId: rubric.model_id,
        ratingScale: agentcore.EvaluatorRatingScale.numerical(
          rubric.rating_scale.numerical.map((r) => ({
            value: r.value,
            label: r.label,
            definition: r.definition,
          })),
        ),
      }),
    });

    // ── 线上持续评估 ────────────────────────────────────────────────────
    //
    // evaluator 的选择对应 docs/PLAN.md 里定的 9 个：
    //   质量 4 个 + 工具 2 个 + 会话 2 个 + 自定义 1 个。
    //   安全（Harmfulness / Stereotyping）作为 self-evolution 的硬门，
    //   在 Phase 5 的本地 gate 里跑；线上这里不占额度（上限 10 个）。
    const onlineEval = new agentcore.OnlineEvaluationConfig(this, 'OnlineEval', {
      onlineEvaluationConfigName: 'selfevolve_demo_online_eval',
      description: 'selfevolve-demo：对订单售后 agent 的线上流量持续评分',

      // 直接绑到 runtime endpoint —— 由后端自己去取 trace，
      // 比手写 log group 名更稳（log group 名会随 runtime id 变）
      dataSource: agentcore.DataSourceConfig.fromAgentRuntimeEndpoint(props.runtime, props.endpoint),

      evaluators: [
        // 质量
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.CORRECTNESS),
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.INSTRUCTION_FOLLOWING),
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.HELPFULNESS),
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.FAITHFULNESS),
        // 工具使用
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.TOOL_SELECTION_ACCURACY),
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.TOOL_PARAMETER_ACCURACY),
        // 会话级
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.GOAL_SUCCESS_RATE),
        // 自定义 —— 直击 v1 的病根
        agentcore.EvaluatorSelector.custom(policyGrounding),
      ],

      // Demo 要每条流量都有分，所以 100%。生产环境应当调低以控成本。
      samplingPercentage: 100,
      executionStatus: agentcore.ExecutionStatus.ENABLED,
    });

    new CfnOutput(this, 'PolicyGroundingEvaluatorId', {
      value: policyGrounding.evaluatorId,
      description: '本地 Phase 2/5 用 manage_evaluations 跑同一个 evaluator 时引用它',
    });
    new CfnOutput(this, 'PolicyGroundingEvaluatorArn', { value: policyGrounding.evaluatorArn });
    new CfnOutput(this, 'OnlineEvalConfigId', { value: onlineEval.onlineEvaluationConfigId });
  }
}
