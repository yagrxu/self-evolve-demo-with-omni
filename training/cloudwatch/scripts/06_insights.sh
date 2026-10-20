#!/usr/bin/env bash
# Step 6 —— 失败分类：用 AgentCore 内置 insight 把失败聚类，并给出每类的修复层（fixType）。
#
#   bash training/cloudwatch/scripts/06_insights.sh baseline control 1 [2 3 ...]   # 可合并多个批次
#
# 这一步回答「该改哪一层」。看两样东西：
#   1. 普遍性：每个根因影响多少个 session（跨批次合并后，k/批次数 才能区分系统性 vs 偶发）
#   2. fixType：SYSTEM_PROMPT_FIX / TOOL_DESCRIPTION_FIX / ... —— 是 insight 给的建议，
#      要结合 trace 里的工具返回人工复核（例如「心算错」被归为 prompt，但更好的修法是工具直接返回金额）
# 合法的 insight id（实测）：Builtin.Insight.FailureAnalysis / UserIntent / ExecutionSummary。
# 注意：insights 与 evaluators 不能放在同一个 batch evaluation 里。
source "$(dirname "$0")/lib/common.sh"
ARM="${1:?arm}"; ROLE="${2:?set}"; shift 2
BATCHES=("$@"); (( ${#BATCHES[@]} )) || die "至少给一个批次号"

sessions=(); s_min=9999999999; e_max=0
for b in "${BATCHES[@]}"; do
  TR="$RUN_DIR/traffic/$ARM-$ROLE-b$b.jsonl"; [[ -f "$TR" ]] || die "缺 $TR"
  while read -r sid; do sessions+=("$sid"); done < <(jq -r 'select(.status=="ok").session_id' "$TR")
  m="${TR%.jsonl}.meta.json"; s=$(jq .window_start_s "$m"); e=$(jq .window_end_s "$m")
  (( s < s_min )) && s_min=$s; (( e > e_max )) && e_max=$e
done
NAME="${RUN_ID}_${ARM}_${ROLE}_insight_$(date +%H%M%S)"
REQ="$RUN_DIR/insights/$ARM-$ROLE.request.json"; mkdir -p "$(dirname "$REQ")"
jq -n --arg n "$NAME" --arg svc "$(service_name "$ARM")" --arg spans "$SPANS_LOG_GROUP" --arg rlg "$(runtime_log_group "$ARM")" \
      --arg t0 "$(iso_utc $((s_min-120)))" --arg t1 "$(iso_utc $((e_max+600)))" \
      --argjson sids "$(printf '%s\n' "${sessions[@]}" | jq -R . | jq -s .)" \
  '{batchEvaluationName:$n,
    insights:[{insightId:"Builtin.Insight.FailureAnalysis"},{insightId:"Builtin.Insight.UserIntent"},{insightId:"Builtin.Insight.ExecutionSummary"}],
    dataSourceConfig:{cloudWatchLogs:{serviceNames:[$svc],logGroupNames:[$spans,$rlg],
      filterConfig:{sessionIds:$sids,timeRange:{startTime:$t0,endTime:$t1}}}}}' > "$REQ"

RES="$RUN_DIR/insights/$ARM-$ROLE.result.json"
if [[ "${SUMMARIZE_ONLY:-}" == 1 && -f "$RES" ]]; then   # 只重算汇总，不再启动新的分析
  st=$(jq -r .status "$RES"); ok "沿用 ${RES}"
else
  log "启动 insight 分析 ${NAME}（${#sessions[@]} 个 session，批次 ${BATCHES[*]}）"
  BID=$(aws bedrock-agentcore start-batch-evaluation --cli-input-json "file://$REQ" --query batchEvaluationId --output text)
  ok "id = $BID"
  st=$(wait_batch_eval "$BID")
  aws bedrock-agentcore get-batch-evaluation --batch-evaluation-id "$BID" --output json > "$RES"
fi
[[ "$st" == COMPLETED* ]] || die "insight 结束状态 ${st}：$(jq -c .errorDetails "$RES")"

# 汇总成「根因 × 修复层 × 普遍性」表（对话 / 截图用；只报条数）
python3 - "$RES" "${#BATCHES[@]}" > "$RUN_DIR/insights/$ARM-$ROLE.classification.json" <<'EOF'
import json,sys,collections
r=json.load(open(sys.argv[1])); nb=int(sys.argv[2])
rows=[]
for f in (r.get("failureAnalysisResult") or {}).get("failures",[]):
    for sub in f.get("subCategories",[]):
        for rc in sub.get("rootCauses",[]):
            # 同一个 session 可能因多个失败 span 重复出现 —— 按 session 去重
            uniq=list({s["sessionId"]:s for s in rc.get("affectedSessions",[])}.values())
            fix=collections.Counter(s.get("fixType","?") for s in uniq)
            # session id = cwt-<run>-<arm>-<scenario>-b<batch>[-0…]；scenario 在第 4 段
            scen=collections.Counter(s["sessionId"].split("-")[3] for s in uniq)
            rows.append({"failure":f["name"],"sub":sub["name"],"root_cause":rc["name"],
                         "sessions":len(uniq),"fix_type":dict(fix),
                         "scenarios":{k:f"{v}/{nb}" for k,v in scen.items()},
                         "recommendation":rc.get("recommendation","")})
by_fix=collections.Counter()
for x in rows:
    for k,v in x["fix_type"].items(): by_fix[k]+=v
print(json.dumps({"root_causes":sorted(rows,key=lambda x:-x["sessions"]),"by_fix_type":dict(by_fix),
                  "user_intents":[{"name":u["name"],"sessions":u["affectedSessionCount"]} for u in (r.get("userIntentResult") or {}).get("userIntents",[])]},
                 ensure_ascii=False,indent=2))
EOF
C="$RUN_DIR/insights/$ARM-$ROLE.classification.json"
echo; echo "修复层分布（按受影响 session 计）："; jq -r '.by_fix_type | to_entries[] | "  \(.key): \(.value)"' "$C"
echo; echo "根因（按普遍性排序）："
jq -r '.root_causes[] | "  [\(.sessions)] \(.root_cause)  → \(.fix_type|keys|join(","))\n       scenario: \(.scenarios|to_entries|map("\(.key) \(.value)")|join(", "))"' "$C"
log "产物：${C}。下一步：人工复核 trace 后决定本轮只改哪一层 → 07_recommend.sh 或手写候选"
