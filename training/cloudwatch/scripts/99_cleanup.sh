#!/usr/bin/env bash
# 清理本次 RUN_ID 创建的资源：候选 stack、数据集、batch evaluation 记录。
# **不会**删除 baseline stack 和 eval stack（它们是"生产"）。
#
#   bash training/cloudwatch/scripts/99_cleanup.sh            # 只列出
#   CONFIRM=yes bash training/cloudwatch/scripts/99_cleanup.sh # 真删
source "$(dirname "$0")/lib/common.sh"
DO="${CONFIRM:-no}"

# 先删候选的默认评估 stack（它 import 了 agent stack 的输出），再删 agent stack
for prefix in "${STACK_PREFIX}-eval-" "${STACK_PREFIX}-agent-"; do
  log "候选 stack（${prefix}*，不含生产 baseline / eval）"
  stacks=$(aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --query "StackSummaries[?starts_with(StackName,'${prefix}')].StackName" --output text)
  for s in $stacks; do
    echo "  $s"
    if [[ "$DO" == yes ]]; then
      aws cloudformation delete-stack --stack-name "$s" && aws cloudformation wait stack-delete-complete --stack-name "$s" && ok "已删：$s"
    fi
  done
done

log "数据集（${RUN_ID}_*）"
for d in $(aws bedrock-agentcore-control list-datasets --query "datasets[?starts_with(datasetName,'${RUN_ID}_')].datasetId" --output text); do
  echo "  $d"
  [[ "$DO" == yes ]] && aws bedrock-agentcore-control delete-dataset --dataset-id "$d" >/dev/null && ok "已删：$d"
done

log "batch evaluation（${RUN_ID}_*）"
for b in $(aws bedrock-agentcore list-batch-evaluations --query "batchEvaluations[?starts_with(batchEvaluationName,'${RUN_ID}_')].batchEvaluationId" --output text 2>/dev/null); do
  echo "  $b"
  [[ "$DO" == yes ]] && aws bedrock-agentcore delete-batch-evaluation --batch-evaluation-id "$b" >/dev/null && ok "已删：$b"
done
log "在线评估 config（${RUN_ID}_*_online）与执行角色"
for c in $(aws bedrock-agentcore-control list-online-evaluation-configs --query "onlineEvaluationConfigs[?starts_with(onlineEvaluationConfigName,'${RUN_ID}_')].onlineEvaluationConfigName" --output text); do
  arm=${c#${RUN_ID}_}; arm=${arm%_online}; echo "  $c（arm $arm）"
  [[ "$DO" == yes ]] && CONFIRM=yes bash "$(dirname "$0")/08_online_eval.sh" delete "$arm"
done
[[ "$DO" == yes ]] || warn "只列出，未删除。确认请加 CONFIRM=yes"
