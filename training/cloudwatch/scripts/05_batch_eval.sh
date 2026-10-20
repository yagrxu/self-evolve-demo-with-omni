#!/usr/bin/env bash
# Step 5 —— 对一个批次做带标准答案的批量评估（AgentCore Batch Evaluation），并导出逐 session 结果。
#
#   bash training/cloudwatch/scripts/05_batch_eval.sh baseline control 1
#
# 标尺来自 gates.json（run 之前登记的 evaluator 列表）。ground truth 按 session 注入：
# 每个 session 的 expected_response / assertions / expected_trajectory 来自数据集快照，
# 对齐键是 session id 里编码的 scenario_id —— 绝不按位置匹配。
source "$(dirname "$0")/lib/common.sh"
ARM="${1:?arm}"; ROLE="${2:?set}"; BATCH="${3:?batch}"
TR="$RUN_DIR/traffic/$ARM-$ROLE-b$BATCH.jsonl"; META="${TR%.jsonl}.meta.json"
[[ -f "$TR" ]] || die "先跑 04_traffic.sh $ARM $ROLE $BATCH"
SNAP="$RUN_DIR/dataset-$ROLE.snapshot.json"
NAME="${RUN_ID}_${ARM}_${ROLE}_b${BATCH}_$(date +%H%M%S)"
REQ="$RUN_DIR/batch/$ARM-$ROLE-b$BATCH.request.json"; mkdir -p "$(dirname "$REQ")"

s=$(jq .window_start_s "$META"); e=$(jq .window_end_s "$META")
python3 - "$TR" "$SNAP" "$GATES_FILE" "$NAME" "$(service_name "$ARM")" "$(runtime_log_group "$ARM")" \
          "$SPANS_LOG_GROUP" "$(iso_utc $((s-120)))" "$(iso_utc $((e+600)))" > "$REQ" <<'EOF'
import json,sys
tr,snap,gates,name,svc,rlg,spans,t0,t1=sys.argv[1:]
ex={x["scenario_id"]:x for x in json.load(open(snap))["examples"]}
g=json.load(open(gates))
evs=g["quality_evaluators"]+g["guard_evaluators"]+g["safety_evaluators"]+g.get("report_only",[])
rows=[json.loads(l) for l in open(tr) if l.strip()]
rows=[r for r in rows if r["status"]=="ok"]
meta=[]
for r in rows:
    x=ex[r["scenario_id"]]
    meta.append({"sessionId":r["session_id"],"testScenarioId":r["scenario_id"],"groundTruth":{"inline":{
        "assertions":[{"text":a} for a in x.get("assertions",[])],
        "expectedTrajectory":{"toolNames":x.get("expected_trajectory",[])},
        "turns":[{"input":{"prompt":t["input"]},"expectedResponse":{"text":t["expected_response"]}} for t in x["turns"]]}}})
print(json.dumps({"batchEvaluationName":name,
  "evaluators":[{"evaluatorId":e} for e in evs],
  "dataSourceConfig":{"cloudWatchLogs":{"serviceNames":[svc],"logGroupNames":[spans,rlg],
     "filterConfig":{"sessionIds":[r["session_id"] for r in rows],"timeRange":{"startTime":t0,"endTime":t1}}}},
  "evaluationMetadata":{"sessionMetadata":meta}},ensure_ascii=False))
EOF
log "启动 batch evaluation ${NAME}（$(jq '.evaluators|length' "$REQ") 个 evaluator × $(jq '.evaluationMetadata.sessionMetadata|length' "$REQ") 个 session）"
BID=$(aws bedrock-agentcore start-batch-evaluation --cli-input-json "file://$REQ" --query batchEvaluationId --output text)
ok "id = $BID"
st=$(wait_batch_eval "$BID")
aws bedrock-agentcore get-batch-evaluation --batch-evaluation-id "$BID" --output json > "$RUN_DIR/batch/$ARM-$ROLE-b$BATCH.result.json"
jq '{status, sessions:.evaluationResults | {total:.totalNumberOfSessions, completed:.numberOfSessionsCompleted, failed:.numberOfSessionsFailed},
     averages:[.evaluationResults.evaluatorSummaries[] | {(.evaluatorId): .statistics.averageScore}] | add}' \
   "$RUN_DIR/batch/$ARM-$ROLE-b$BATCH.result.json"
[[ "$st" == COMPLETED* ]] || die "batch evaluation 结束状态 $st"
total=$(jq .evaluationResults.totalNumberOfSessions "$RUN_DIR/batch/$ARM-$ROLE-b$BATCH.result.json")
[[ "$total" != "0" ]] || die "0 个 session —— span 还没落地，或 serviceName/log group 不对（不是'没流量'）"

log "导出逐 session 结果（$BATCH_RESULTS_LOG_GROUP / run-${BID}）"
OUT="$RUN_DIR/results/$ARM-$ROLE-b$BATCH.jsonl"; mkdir -p "$(dirname "$OUT")"; : > "$OUT"
tok=""
while :; do
  page=$(aws logs get-log-events --log-group-name "$BATCH_RESULTS_LOG_GROUP" --log-stream-name "run-$BID" \
           --start-from-head ${tok:+--next-token "$tok"} --output json)
  echo "$page" | jq -c '.events[].message | fromjson
     | {session_id:.attributes["session.id"], trace_id:.traceId,
        evaluator:.attributes["gen_ai.evaluation.name"], score:.attributes["gen_ai.evaluation.score.value"],
        explanation:(.attributes["gen_ai.evaluation.explanation"] // "")[0:800]}' >> "$OUT"
  nt=$(echo "$page" | jq -r .nextForwardToken); [[ "$nt" == "$tok" ]] && break; tok="$nt"
done
ok "$(wc -l < "$OUT" | tr -d ' ') 条逐 session 评分 → $OUT"
log "导出 token 用量（aws/spans 里 invoke_agent span 的 gen_ai.usage.*）"
TOK="$RUN_DIR/results/$ARM-$ROLE-b$BATCH.tokens.jsonl"; : > "$TOK"
tok=""
while :; do
  page=$(aws logs filter-log-events --log-group-name "$SPANS_LOG_GROUP" \
          --start-time $(( (s-120)*1000 )) --end-time $(( (e+600)*1000 )) \
          --filter-pattern "\"cwt-${RUN_ID}-${ARM}-\" \"invoke_agent\"" ${tok:+--next-token "$tok"} --output json)
  echo "$page" | jq -c '.events[].message | fromjson | select(.name|startswith("invoke_agent"))
     | {session_id:.attributes["session.id"],
        input_tokens:(.attributes["gen_ai.usage.input_tokens"] // 0),
        output_tokens:(.attributes["gen_ai.usage.output_tokens"] // 0)}' >> "$TOK"
  tok=$(echo "$page" | jq -r '.nextToken // empty'); [[ -z "$tok" ]] && break
done
# 只保留本批次的 session（同一 arm 的相邻批次时间窗口会重叠），每个 session 一条
python3 - "$TOK" "$TR" <<'PY'
import json,sys
tok,tr=sys.argv[1:]
want={json.loads(l)["session_id"] for l in open(tr) if l.strip()}
seen={}
for l in open(tok):
    if l.strip():
        r=json.loads(l)
        if r["session_id"] in want: seen[r["session_id"]]=r
open(tok,"w").write("".join(json.dumps(r)+"\n" for r in seen.values()))
PY
ok "$(wc -l < "$TOK" | tr -d ' ') 个 session 的 token 用量 → $TOK"
jq -n --arg id "$BID" --arg st "$st" '{batch_evaluation_id:$id,status:$st}' > "$RUN_DIR/batch/$ARM-$ROLE-b$BATCH.json"
log "下一步：06_insights.sh $ARM $ROLE ${BATCH}（失败分类）或 09_gates.py（比较两个 arm）"
