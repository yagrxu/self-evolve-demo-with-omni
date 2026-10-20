#!/usr/bin/env bash
# Step 8 —— 导出在线评分；以及（补救用）给**不是由本 CDK 部署的** runtime 配置默认评估。
#
# 正常路径：01_deploy.sh <arm> 已经用 CDK 同时部署了 selfevolve-demo-eval-<arm>（默认评估）。
# 只有在 runtime 不是这套 CDK 部署的、或需要在已有 runtime 上临时补一份时，才用这里的 create。
#
# 为什么需要：生产的 online evaluation config 只绑定 baseline runtime 的 log group，
# 候选是独立 runtime，它们的流量默认**不会**被在线评估。S2（云上灰度，真实流量没有标准答案）
# 唯一的评分来源就是在线评估 —— 所以 S2 之前要让候选与对照都被同一套 evaluator 覆盖。
#
#   bash 08_online_eval.sh create  <arm> [--dry-run]   # 建执行角色 + online evaluation config（与生产同一套 evaluator）
#   bash 08_online_eval.sh status  <arm>
#   bash 08_online_eval.sh export  <arm> <set> <batch>  # 在线评分 → results/<arm>-<set>-b<batch>.online.jsonl
#   CONFIRM=yes bash 08_online_eval.sh delete <arm>
#
# 实测要点：
#  · evaluator 列表直接复制生产 config（不另起一套标尺）
#  · 执行角色只授权读该 arm 的 runtime log group + aws/spans，写 /aws/bedrock-agentcore/evaluations/*
#  · 在线评估要等 session 空闲超过 sessionTimeoutMinutes（默认 15 分钟）才开始打分
source "$(dirname "$0")/lib/common.sh"
CMD="${1:?create|status|export|delete}"; ARM="${2:?arm}"
PROD_CFG="${PROD_ONLINE_EVAL_CONFIG_ID:-selfevolve_demo_online_eval-b8cUsWHGnR}"
NAME="${RUN_ID}_${ARM}_online"                       # [a-zA-Z][a-zA-Z0-9_]{0,47}
ROLE="selfevolve-demo-online-eval-${RUN_ID}-${ARM//_/-}"
REC="$RUN_DIR/online-eval-$ARM.json"
ACC=$(aws sts get-caller-identity --query Account --output text)

cfg_id() { aws bedrock-agentcore-control list-online-evaluation-configs \
             --query "onlineEvaluationConfigs[?onlineEvaluationConfigName=='$NAME'].onlineEvaluationConfigId | [0]" --output text; }

case "$CMD" in
create)
  DRY=""; [[ "${3:-}" == "--dry-run" ]] && DRY=1
  LG=$(runtime_log_group "$ARM"); SVC=$(service_name "$ARM")
  log "生产 config $PROD_CFG 的 evaluator 列表（照抄，保证同一把尺子）"
  EVS=$(aws bedrock-agentcore-control get-online-evaluation-config --online-evaluation-config-id "$PROD_CFG" \
          --query 'evaluators' --output json)
  echo "$EVS" | jq -r '.[].evaluatorId' | sed 's/^/    /'
  TRUST=$(jq -cn --arg a "$ACC" --arg r "$AWS_REGION" '{Version:"2012-10-17",Statement:[{Effect:"Allow",
    Principal:{Service:"bedrock-agentcore.amazonaws.com"},Action:"sts:AssumeRole",
    Condition:{StringEquals:{"aws:SourceAccount":$a,"aws:ResourceAccount":$a},
               ArnLike:{"aws:SourceArn":["arn:aws:bedrock-agentcore:\($r):\($a):evaluator/*","arn:aws:bedrock-agentcore:\($r):\($a):online-evaluation-config/*"]}}}]}')
  POL=$(jq -cn --arg a "$ACC" --arg r "$AWS_REGION" --arg lg "$LG" '{Version:"2012-10-17",Statement:[
    {Effect:"Allow",Action:"logs:DescribeLogGroups",Resource:"*"},
    {Effect:"Allow",Action:["logs:StartQuery","logs:GetQueryResults"],Resource:[
      "arn:aws:logs:\($r):\($a):log-group:\($lg)","arn:aws:logs:\($r):\($a):log-group:\($lg):*",
      "arn:aws:logs:\($r):\($a):log-group:aws/spans","arn:aws:logs:\($r):\($a):log-group:aws/spans:*"]},
    {Effect:"Allow",Action:["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
     Resource:"arn:aws:logs:\($r):\($a):log-group:/aws/bedrock-agentcore/evaluations/*"},
    {Effect:"Allow",Action:["logs:DescribeIndexPolicies","logs:PutIndexPolicy"],Resource:[
      "arn:aws:logs:\($r):\($a):log-group:aws/spans","arn:aws:logs:\($r):\($a):log-group:aws/spans:*"]},
    {Effect:"Allow",Action:["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],Resource:[
      "arn:aws:bedrock:*:\($a):inference-profile/*","arn:aws:bedrock:*::foundation-model/*"]}]}')
  REQ=$(jq -n --arg n "$NAME" --arg lg "$LG" --arg svc "$SVC" --argjson evs "$EVS" \
          --arg role "arn:aws:iam::${ACC}:role/${ROLE}" --arg d "training ${RUN_ID}: ${ARM} 的在线评估（evaluator 与生产一致）" \
    '{onlineEvaluationConfigName:$n,description:$d,
      rule:{samplingConfig:{samplingPercentage:100},sessionConfig:{sessionTimeoutMinutes:15}},
      dataSourceConfig:{cloudWatchLogs:{logGroupNames:[$lg],serviceNames:[$svc]}},
      evaluators:$evs,evaluationExecutionRoleArn:$role,enableOnCreate:true}')
  echo "$REQ" > "$RUN_DIR/online-eval-$ARM.request.json"
  echo "  role   : $ROLE"; echo "  log grp: $LG"; echo "  service: $SVC"
  if [[ -n "$DRY" ]]; then ok "dry-run：请求已写到 $RUN_DIR/online-eval-$ARM.request.json，未创建任何资源"; exit 0; fi
  existing=$(cfg_id); if [[ "$existing" != "None" && -n "$existing" ]]; then ok "已存在：$existing"; exit 0; fi
  if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
      --tags Key=Project,Value=selfevolve-demo-with-omni Key=RunId,Value="$RUN_ID" >/dev/null
    ok "已建执行角色 $ROLE"
  fi
  aws iam put-role-policy --role-name "$ROLE" --policy-name online-eval --policy-document "$POL"
  log "等待 IAM 生效（15s）"; sleep 15
  for i in 1 2 3 4; do
    if out=$(aws bedrock-agentcore-control create-online-evaluation-config --cli-input-json "$REQ" --output json 2>&1); then break; fi
    warn "第 $i 次创建失败（多为 IAM 传播延迟）：${out:0:160}"; sleep 15
  done
  echo "$out" | jq '{id:.onlineEvaluationConfigId,status,executionStatus,out:.outputConfig.cloudWatchConfig.logGroupName}' | tee "$REC"
  ;;
status)
  id=$(cfg_id); [[ "$id" != "None" ]] || die "没有 $NAME"
  aws bedrock-agentcore-control get-online-evaluation-config --online-evaluation-config-id "$id" \
    --query '{id:onlineEvaluationConfigId,status:status,exec:executionStatus,source:dataSourceConfig.cloudWatchLogs,out:outputConfig.cloudWatchConfig.logGroupName,evaluators:length(evaluators)}'
  ;;
export)
  SET="${3:?set}"; B="${4:?batch}"
  id=$(cfg_id); [[ "$id" != "None" ]] || die "没有 $NAME"
  OLG=$(aws bedrock-agentcore-control get-online-evaluation-config --online-evaluation-config-id "$id" --query outputConfig.cloudWatchConfig.logGroupName --output text)
  [[ "$ARM" == "baseline" ]] && OLG=$(aws bedrock-agentcore-control get-online-evaluation-config --online-evaluation-config-id "$PROD_CFG" --query outputConfig.cloudWatchConfig.logGroupName --output text)
  META="$RUN_DIR/traffic/$ARM-$SET-b$B.meta.json"; s=$(jq .window_start_s "$META"); e=$(jq .window_end_s "$META")
  OUT="$RUN_DIR/results/$ARM-$SET-b$B.online.jsonl"; mkdir -p "$(dirname "$OUT")"
  aws logs filter-log-events --log-group-name "$OLG" --start-time $(( (s-60)*1000 )) --end-time $(( (e+7200)*1000 )) \
      --filter-pattern "\"cwt-${RUN_ID}-${ARM}-\"" --output json \
    | jq -c '.events[].message | fromjson | {session_id:.attributes["session.id"], trace_id:.traceId,
        evaluator:(.attributes["gen_ai.evaluation.name"]|sub("^selfevolve_demo_";"")), score:.attributes["gen_ai.evaluation.score.value"],
        explanation:(.attributes["gen_ai.evaluation.explanation"] // "")[0:400]}' > "$OUT"
  want=$(jq -r 'select(.status=="ok").session_id' "$RUN_DIR/traffic/$ARM-$SET-b$B.jsonl" | sort -u | wc -l | tr -d ' ')
  got=$(jq -r .session_id "$OUT" | sort -u | wc -l | tr -d ' ')
  ok "$got / $want 个 session 有在线评分 → $OUT"
  (( got == want )) || warn "在线评估是异步的：session 空闲 15 分钟后才打分，未覆盖完就稍后重跑 export"
  ;;
delete)
  [[ "${CONFIRM:-}" == yes ]] || die "删除需要 CONFIRM=yes"
  id=$(cfg_id); [[ "$id" != "None" ]] && aws bedrock-agentcore-control delete-online-evaluation-config --online-evaluation-config-id "$id" >/dev/null && ok "已删 config $id"
  aws iam delete-role-policy --role-name "$ROLE" --policy-name online-eval 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE" 2>/dev/null && ok "已删角色 $ROLE" || true
  ;;
*) die "未知子命令 $CMD" ;;
esac
