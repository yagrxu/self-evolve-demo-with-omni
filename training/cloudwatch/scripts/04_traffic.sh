#!/usr/bin/env bash
# Step 4 —— 让某个 arm 把某份数据集完整跑一遍（一个"批次"）。
#
#   bash training/cloudwatch/scripts/04_traffic.sh baseline control 1
#   bash training/cloudwatch/scripts/04_traffic.sh c1       control 1
#
# 为什么是"批次"而不是"重复 3 次"：实测同一时段内的重复高度相关
# （同一条题在一次 run 里 3/3 同错、换个时段 3/3 同对）。所以重复要**分批**做：
# 每个批次是一次独立调用 + 独立 session；baseline 与候选按批次交替跑，配对才成立。
source "$(dirname "$0")/lib/common.sh"
ARM="${1:?arm}"; ROLE="${2:?control|confirm}"; BATCH="${3:?batch 序号}"
ARN=$(runtime_arn "$ARM")
SNAP="$RUN_DIR/dataset-$ROLE.snapshot.json"
[[ -f "$SNAP" ]] || die "先跑 03_dataset.sh $ROLE"
OUT="$RUN_DIR/traffic/$ARM-$ROLE-b$BATCH.jsonl"
mkdir -p "$(dirname "$OUT")"; : > "$OUT"

N=$(jq '.examples|length' "$SNAP")
log "$ARM × $ROLE 批次 ${BATCH}：$N 条 → $ARN"
start=$(date +%s)
for i in $(seq 0 $((N-1))); do
  sc=$(jq -r ".examples[$i].scenario_id" "$SNAP")
  prompt=$(jq -r ".examples[$i].turns[0].input" "$SNAP")
  sid=$(session_id "$ARM" "$sc" "$BATCH")
  t0=$(now_ms)
  if aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn "$ARN" --qualifier "$ENDPOINT_NAME" \
       --runtime-session-id "$sid" --content-type application/json --accept application/json \
       --cli-binary-format raw-in-base64-out \
       --payload "$(jq -cn --arg p "$prompt" --arg s "$sid" '{prompt:$p,sessionId:$s}')" \
       "$RUN_DIR/traffic/.last.out" >/dev/null 2>"$RUN_DIR/traffic/.last.err"; then
    status=ok
  else
    status=error
  fi
  t1=$(now_ms)
  jq -cn --arg sid "$sid" --arg sc "$sc" --arg arm "$ARM" --arg role "$ROLE" --argjson b "$BATCH" \
     --arg st "$status" --argjson lat $((t1-t0)) --argjson t0 "$t0" \
     --rawfile out "$RUN_DIR/traffic/.last.out" \
     '{session_id:$sid,scenario_id:$sc,arm:$arm,set:$role,batch:$b,status:$st,latency_ms:$lat,sent_at_ms:$t0,output:$out}' >> "$OUT"
  printf '  [%2d/%d] %s %s %6dms\n' $((i+1)) "$N" "$([[ $status == ok ]] && echo ✓ || echo ✗)" "$sc" $((t1-t0)) >&2
done
end=$(date +%s)
okc=$(jq -s 'map(select(.status=="ok"))|length' "$OUT")
jq -n --arg arm "$ARM" --arg role "$ROLE" --argjson b "$BATCH" --argjson s "$start" --argjson e "$end" --argjson ok "$okc" --argjson n "$N" \
  '{arm:$arm,set:$role,batch:$b,window_start_s:$s,window_end_s:$e,ok:$ok,total:$n}' > "${OUT%.jsonl}.meta.json"
ok "$okc/$N 成功；窗口 $(iso_utc "$start") – $(iso_utc "$end")"
(( okc == N )) || warn "有失败调用 —— 它们不会进入评估（未评分 ≠ 0 分）"
log "下一步：05_batch_eval.sh $ARM $ROLE ${BATCH}（span 落地需要 1–3 分钟）"
