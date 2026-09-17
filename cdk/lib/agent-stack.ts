import * as path from 'path';
import { CfnOutput, Duration, Stack, StackProps } from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import {
  BASELINE_MODEL_ID,
  PREFIX,
  ENABLE_COUPON_TOOL,
  ENDPOINT_NAME,
  ENTRYPOINT,
  PROMPT_VARIANT,
  PYTHON_RUNTIME,
  RUNTIME_NAME,
} from './config';

const REPO_ROOT = path.resolve(__dirname, '..', '..');

/**
 * AgentCore Runtime + Endpoint，用 **Direct Code Deploy**（S3 zip，无 Docker）。
 *
 * 为什么不用容器
 * --------------
 * `AWS::BedrockAgentCore::Runtime` 的 `AgentRuntimeArtifact` 有两条路：
 * `ContainerConfiguration`（ECR）和 `CodeConfiguration`（S3 zip + 托管 Python）。
 * 本 Demo 走后者：不需要本地 Docker daemon，`cdk deploy` 明显更快 ——
 * 而"改完 prompt 立刻重新部署"在演示里是高频动作。
 *
 * 依赖打包
 * --------
 * 托管运行时**不会**替你 pip install，依赖必须已经躺在 zip 里。
 * 打包由 `scripts/build_agent_bundle.sh` 完成，产物在 `agent/.build/`。
 * 这个 stack 只负责把那个目录变成 S3 asset —— 所以 **deploy 前必须先跑打包脚本**
 * （`npm run deploy` 之前执行，或用仓库根目录的 Makefile 目标）。
 *
 * asset hash 会随 `agent/.build/` 内容变化，因此改 prompt / 换模型 / 加工具
 * 都会自动产生 runtime 的新版本 —— 这正是 Phase 6 重新部署所依赖的机制。
 */
export class AgentStack extends Stack {
  public readonly runtime: agentcore.Runtime;
  public readonly endpoint: agentcore.RuntimeEndpoint;

  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    // ── 执行角色 ────────────────────────────────────────────────────────
    // 手写而不用 L2 自动生成的，是为了让"agent 到底被授了什么权"在代码里一眼可见 ——
    // 这对可追溯性比少写几行更重要。
    const executionRole = new iam.Role(this, 'ExecutionRole', {
      roleName: `${RUNTIME_NAME}-exec-${this.region}`,
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: { 'aws:SourceArn': `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*` },
        },
      }),
      // CFN 对 IAM Role 的 Description 限定为 ASCII/Latin-1 字符集，
      // 中文会触发 CloudFormation-Validate::F3031 告警，可能导致部署失败。
      // 所以只有 role description 用英文，其余描述照常用中文。
      description: 'Execution role for the selfevolve-demo order-support AgentCore Runtime',
    });

    // 模型调用。同时授 foundation-model 和 inference-profile：
    // prompts.json 里的 modelId 带 `us.` 前缀（跨区推理配置），走的是后者，
    // 但底层仍需前者 —— 只授一个会在运行时报 AccessDenied。
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeBedrockModels',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:*::foundation-model/anthropic.*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/*anthropic.*`,
        ],
      }),
    );

    // 可观测性。omni-production-observability skill 要求的那组权限：
    // OTLP 走 cloudwatch:Ingest + CallWithBearerToken，X-Ray 段走 xray:Put*。
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EmitTelemetry',
        actions: [
          'cloudwatch:PutMetricData',
          'cloudwatch:Ingest',
          'cloudwatch:CallWithBearerToken',
          'xray:PutTraceSegments',
          'xray:PutTelemetryRecords',
          'xray:GetSamplingRules',
          'xray:GetSamplingTargets',
        ],
        resources: ['*'], // 这些 action 本身不支持资源级限定
      }),
    );

    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'WriteRuntimeLogs',
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*`,
        ],
      }),
    );

    // AgentCore 给 runtime 签发 workload identity 时需要
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'WorkloadIdentity',
        actions: ['bedrock-agentcore:GetWorkloadAccessToken'],
        resources: ['*'],
      }),
    );

    // ── Runtime ─────────────────────────────────────────────────────────
    this.runtime = new agentcore.Runtime(this, 'Runtime', {
      runtimeName: RUNTIME_NAME,
      description: 'selfevolve-demo 订单售后客服 agent (Strands, Direct Code Deploy)',
      executionRole,

      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromCodeAsset({
        // 必须是打包脚本的产物目录，不是 agent/ 本身 —— agent/ 里没有 vendored 依赖
        path: path.join(REPO_ROOT, 'agent', '.build'),
        runtime: agentcore.AgentCoreRuntime.of(PYTHON_RUNTIME),
        entrypoint: ENTRYPOINT,
      }),

      // BedrockAgentCoreApp 暴露的是普通 HTTP /invocations，不是 MCP/A2A
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),

      // 让 L2 帮我们把 trace/log 投递配好 —— 这是云端 trace 能被
      // Omni 的 search_agent_traces 查到的前提
      tracingEnabled: true,

      environmentVariables: {
        // skill omni-production-observability 明确要求：不让平台抽取 GenAI 内容，
        // 由我们自己的 ADOT/OpenInference span 属性负责语义
        AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT: 'true',
        AGENT_OBSERVABILITY_ENABLED: 'true',

        AWS_REGION: this.region,
        MODEL_ID: BASELINE_MODEL_ID,

        // Phase 5 三条优化轴的开关（prompt 本身在包内的 prompts.json 里）
        AGENT_PROMPT_VARIANT: PROMPT_VARIANT,
        ENABLE_COUPON_TOOL: ENABLE_COUPON_TOOL,

        // 固定"今天"，保证云上和本地算出的 oracle 完全一致
        DEMO_AS_OF_DATE: '2026-09-17',
        LOG_LEVEL: 'INFO',
      },

      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: Duration.minutes(15),
        maxLifetime: Duration.hours(8),
      },
    });

    // ── Endpoint ────────────────────────────────────────────────────────
    // 独立的命名 endpoint（而非 DEFAULT）：Phase 3 用 v1、Phase 6 用 v2，
    // 两个 endpoint 并存，可以同时打流量做并排对比。
    this.endpoint = this.runtime.addEndpoint(ENDPOINT_NAME, {
      description: `selfevolve-demo endpoint ${ENDPOINT_NAME} (${PROMPT_VARIANT})`,
    });

    new CfnOutput(this, 'RuntimeArn', { value: this.runtime.agentRuntimeArn, description: 'AgentCore Runtime ARN' });
    new CfnOutput(this, 'RuntimeId', {
      value: this.runtime.agentRuntimeId,
      description: '给 scripts/traffic.py 用的 runtime id',
      // CFN 导出名只允许字母数字/冒号/连字符 —— 不能用 RUNTIME_NAME
      // （AgentCore 的 runtime 名规则要求下划线，两边规则冲突）
      exportName: `${PREFIX}-runtime-id`,
    });
    new CfnOutput(this, 'EndpointName', { value: this.endpoint.endpointName });
    new CfnOutput(this, 'ApplicationLogGroup', {
      value: this.runtime.applicationLogGroup.logGroupName,
      description: 'EvalStack 的 online evaluation 从这个 log group 读 trace',
    });
    new CfnOutput(this, 'PromptVariant', { value: PROMPT_VARIANT, description: '本次部署的 prompt 变体标记' });
    new CfnOutput(this, 'ExecutionRoleArn', { value: executionRole.roleArn });
  }
}
