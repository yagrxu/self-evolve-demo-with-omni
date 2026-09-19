#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { AgentStack } from '../lib/agent-stack';
import { EvalStack } from '../lib/eval-stack';
import { PREFIX, REGION } from '../lib/config';

const app = new cdk.App();

/**
 * region 显式写死，account 从环境取（default profile）。
 * 见 docs/BUILD-LOG.md 决策点 R4/R4'：本机 profile 默认 region 是 ap-southeast-1，
 * 而 Omni Space 在 us-west-2 —— 云端 trace 查询走它，所以 runtime 必须部在同一个 region。
 */
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: REGION,
};

const agent = new AgentStack(app, `${PREFIX}-agent`, {
  env,
  description: 'selfevolve-demo：订单售后客服 agent（AgentCore Runtime，Direct Code Deploy）',
});

// evaluator 需要引用 runtime/endpoint，所以 EvalStack 必然依赖 AgentStack。
// 拆成两个 stack 而不是一个：Phase 6 重新部署 agent 时不必动评估配置，
// 反过来调 evaluator 组合时也不会触发 runtime 重建。
new EvalStack(app, `${PREFIX}-eval`, {
  env,
  description: 'selfevolve-demo：自定义 evaluator + 线上持续评估',
  runtime: agent.runtime,
  endpoint: agent.endpoint,
});

cdk.Tags.of(app).add('Project', 'selfevolve-demo-with-omni');
cdk.Tags.of(app).add('ManagedBy', 'cdk');
// 打上明确标记：账号里有别人的 AgentCore demo（cat_demo_strands / cat_demo_langgraph），
// 出问题时能一眼分清谁是谁。
// 注意分隔符用 `/` 不用 `,` —— IAM 的 tag value 字符集 ^[\p{L}\p{Z}\p{N}_.:/=+\-@]*$
// 不含逗号，用逗号会触发 CloudFormation-Validate::F3031。
cdk.Tags.of(app).add('OtherDemosInAccount', 'cat_demo_strands/cat_demo_langgraph');

app.synth();
