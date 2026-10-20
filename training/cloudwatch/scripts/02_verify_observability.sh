#!/usr/bin/env bash
# Step 2 —— 验证可观测性：发 1 条请求，确认 span 真的进了 CloudWatch（aws/spans）。
# 不验证就往下跑，后面所有评估都可能是「0 条 trace」—— 而且不会报错。
#
#   bash training/cloudwatch/scripts/02_verify_observability.sh baseline
source "$(dirname "$0")/lib/common.sh"
ARM="${1:-baseline}"
ARN=$(runtime_arn "$ARM")
SID=$(session_id "$ARM" "smoke" 0)

log "调用 ${ARM}（session ${SID}）"
t0=$(date +%s)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$ARN" --qualifier "$ENDPOINT_NAME" \
  --runtime-session-id "$SID" --content-type application/json --accept application/json \
  --cli-binary-format raw-in-base64-out \
  --payload "$(jq -cn --arg p '订单号 ORD-10101。无线鼠标昨天刚到，我想退掉，能退多少？' --arg s "$SID" '{prompt:$p,sessionId:$s}')" \
  "$RUN_DIR/smoke-$ARM.out" >/dev/null
jq -r '.result // .' "$RUN_DIR/smoke-$ARM.out" | head -5
ok "调用成功"

log "等待 span 落地到 ${SPANS_LOG_GROUP}（最多 5 分钟）"
for i in $(seq 1 20); do
  n=$(aws logs filter-log-events --log-group-name "$SPANS_LOG_GROUP" \
        --start-time $(( (t0-60)*1000 )) --end-time $(( ($(date +%s)+60)*1000 )) \
        --filter-pattern "\"$SID\"" --output json 2>/dev/null | jq '.events|length' || echo 0)
  [[ "$n" != "0" && "$n" != "None" ]] && break
  sleep 15
done
[[ "$n" != "0" && "$n" != "None" ]] || die "5 分钟内没有 span —— 检查 Transaction Search（00_prereq.sh）与 entrypoint 是否带 opentelemetry-instrument"
sleep 20   # 首个 span 到了，其余的通常还在路上
names=$(aws logs filter-log-events --log-group-name "$SPANS_LOG_GROUP" \
  --start-time $(( (t0-60)*1000 )) --end-time $(( ($(date +%s)+60)*1000 )) \
  --filter-pattern "\"$SID\"" --output json | jq -r '.events[].message | fromjson | .name')
echo "$names" | sort | uniq -c
for want in "invoke_agent" "execute_tool" "chat"; do
  grep -q "^$want" <<<"$names" || die "缺少 $want span"
done
ok "span 已落地：$(wc -l <<<"$names" | tr -d ' ') 条，含 invoke_agent / execute_tool / chat"

cat <<EOF

在控制台查看（截图用）：
  CloudWatch → GenAI Observability → Bedrock AgentCore → Agents → $(runtime_name "$ARM")
  https://console.aws.amazon.com/cloudwatch/home?region=$AWS_REGION#/gen-ai-observability/agent-core
  按 session 过滤：$SID
EOF
