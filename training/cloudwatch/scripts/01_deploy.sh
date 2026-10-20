#!/usr/bin/env bash
# Step 1 —— 用 CDK 部署一个 arm（baseline 或某个候选），并开启可观测性。
#
#   bash training/cloudwatch/scripts/01_deploy.sh baseline
#   bash training/cloudwatch/scripts/01_deploy.sh c1 path/to/c1/prompts.json   # 候选：独立 runtime + 独立 stack
#   TOOL_TIER=enhanced bash training/cloudwatch/scripts/01_deploy.sh t1         # 候选：只改工具层
#
# 关键约束：候选**只改一层**。prompt 候选只换 prompts.json；tool 候选只换 AGENT_TOOL_TIER。
# 两者都部署成独立 runtime，baseline 的 endpoint 不受影响。
# 候选会同时部署 selfevolve-demo-eval-<arm>：一份与生产 evaluator 相同的默认评估（Online Evaluation）。
#
# --synth-only：只合成模板、不部署（用于评审）。
source "$(dirname "$0")/lib/common.sh"
ARM="${1:?用法：01_deploy.sh <arm> [prompts.json] [--synth-only]}"
PROMPTS="${2:-}"
MODE="deploy"; [[ " $* " == *" --synth-only "* ]] && MODE="synth"
[[ "$PROMPTS" == "--synth-only" ]] && PROMPTS=""

if [[ "$ARM" == "baseline" ]]; then
  SUFFIX=""
else
  [[ "$ARM" =~ ^[a-z0-9_]+$ ]] || die "arm 名只允许 [a-z0-9_]"
  SUFFIX="$ARM"
fi
# baseline 默认用 BASELINE_PROMPTS（训练材料自带的 v1），保证与「线上 baseline」是同一份
[[ "$ARM" == "baseline" && -z "$PROMPTS" ]] && PROMPTS="$BASELINE_PROMPTS"
[[ -z "$PROMPTS" || -f "$PROMPTS" ]] || die "找不到 $PROMPTS"

log "打包 agent（Direct Code Deploy，依赖 vendored 进 zip）"
PROMPTS_FILE="${PROMPTS:-}" AGENT_BUNDLE_PLATFORM=manylinux2014_aarch64 \
  bash scripts/build_agent_bundle.sh | tail -4
ok "prompt 版本：$(cd agent/.build && python3 -c 'import prompt_loader as p; print(p.get_prompt_version("order_support"), p.get_prompt_hash("order_support"))')"

STACK=$(stack_name "$ARM")
cd cdk
export AGENT_RUNTIME_SUFFIX="$SUFFIX" AGENT_TOOL_TIER="${TOOL_TIER:-basic}"
# baseline 保持 CDK 默认的变体标签（v1-baseline），否则重新部署会改动线上 runtime 的环境变量
[[ "$ARM" == "baseline" ]] || export AGENT_PROMPT_VARIANT="$ARM"
if [[ "$MODE" == "synth" ]]; then
  npx cdk synth "$STACK" --quiet >/dev/null
  ok "已合成 cdk/cdk.out/$STACK.template.json（未部署）"
  exit 0
fi

log "部署 $STACK"
if [[ "$ARM" == "baseline" ]]; then
  npx cdk deploy "$STACK" "${STACK_PREFIX}-eval" --require-approval never
else
  # 候选 / 对照：agent stack + 它自己的默认评估 stack（evaluator 与生产一致）—— 同一次部署
  RUN_ID="$RUN_ID" npx cdk deploy "$STACK" "${STACK_PREFIX}-eval-${ARM//_/-}" --require-approval never
fi
cd "$REPO_ROOT"

arn=$(runtime_arn "$ARM")
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$(runtime_id "$ARM")" \
  --query '{name:agentRuntimeName,status:status,version:agentRuntimeVersion,entry:agentRuntimeArtifact.codeConfiguration.entryPoint}'
ok "$ARM 已部署：$arn"
echo "{\"arm\":\"$ARM\",\"stack\":\"$STACK\",\"runtime_arn\":\"$arn\",\"prompts\":\"${PROMPTS:-agent/prompts.json}\",\"tool_tier\":\"${TOOL_TIER:-basic}\"}" \
  > "$RUN_DIR/deploy-$ARM.json"
log "下一步：02_verify_observability.sh $ARM"
