#!/usr/bin/env bash
# Step 7 —— 生成候选（只改一层）。两种来源，二选一：
#
#   A. 让 AgentCore 基于 trace 推荐新的 system prompt（Recommendation API）
#      bash training/cloudwatch/scripts/07_recommend.sh c1 auto baseline control 1
#   B. 人工撰写（例如针对 06 的某个根因），给一个 prompt 文本文件
#      bash training/cloudwatch/scripts/07_recommend.sh c1 manual path/to/system_prompt.txt
#
# 产物：runs/<RUN_ID>/candidates/<id>/prompts.json —— **完整的独立文件**，
# 除 system prompt 与新增的 history 版本外与 agent/prompts.json 逐键一致（脚本断言）。
# 这保证候选与 baseline 之间唯一的差异就是 prompt（不变量：每轮只改一层）。
#
# 实测约束：Recommendation 的 evaluationConfig 只接受 **1 个** evaluator（取 gates.json 的第一个质量 evaluator）。
# 推荐结果经常是「把计算规则写进 prompt」—— 这正是诊断里 Tool 层的问题，要人工判断是否该改工具而不是 prompt。
source "$(dirname "$0")/lib/common.sh"
CID="${1:?候选 id（[a-z0-9_]）}"; MODE="${2:?auto|manual}"
[[ "$CID" =~ ^[a-z0-9_]+$ ]] || die "候选 id 只允许 [a-z0-9_]"
CD="$RUN_DIR/candidates/$CID"; mkdir -p "$CD"
[[ -f "$BASELINE_PROMPTS" ]] || die "找不到 BASELINE_PROMPTS=$BASELINE_PROMPTS"
BASE_PROMPT="${BASE_PROMPT_OVERRIDE:-$(jq -r '.order_support.messages[] | select(.role=="system").content' "$BASELINE_PROMPTS")}"
ok "底稿：${BASELINE_PROMPTS}（$(jq -r '.order_support.history[-1].versionId' "$BASELINE_PROMPTS")）"

if [[ "$MODE" == "auto" ]]; then
  SRC_ARM="${3:?源 arm}"; ROLE="${4:?set}"; BATCH="${5:?batch}"
  META="$RUN_DIR/traffic/$SRC_ARM-$ROLE-b$BATCH.meta.json"
  ACC=$(aws sts get-caller-identity --query Account --output text)
  s=$(jq .window_start_s "$META"); e=$(jq .window_end_s "$META")
  jq -n --arg n "${RUN_ID}_${CID}_rec_$(date +%H%M%S)" --arg p "$BASE_PROMPT" \
        --arg a1 "arn:aws:logs:${AWS_REGION}:${ACC}:log-group:${SPANS_LOG_GROUP}" \
        --arg a2 "arn:aws:logs:${AWS_REGION}:${ACC}:log-group:$(runtime_log_group "$SRC_ARM")" \
        --arg svc "$(service_name "$SRC_ARM")" --arg t0 "$(iso_utc $((s-120)))" --arg t1 "$(iso_utc $((e+600)))" \
        --argjson evs "$(jq '[.quality_evaluators[0] | {evaluatorArn:("arn:aws:bedrock-agentcore:::evaluator/"+.)}]' "$GATES_FILE")" \
    '{name:$n,type:"SYSTEM_PROMPT_RECOMMENDATION",recommendationConfig:{systemPromptRecommendationConfig:{
        systemPrompt:{text:$p},
        agentTraces:{cloudwatchLogs:{logGroupArns:[$a1,$a2],serviceNames:[$svc],startTime:$t0,endTime:$t1}},
        evaluationConfig:{evaluators:$evs}}}}' > "$CD/recommendation.request.json"
  log "启动 system prompt 推荐（基于 $SRC_ARM × $ROLE 批次 $BATCH 的 trace）"
  RID=$(aws bedrock-agentcore start-recommendation --cli-input-json "file://$CD/recommendation.request.json" \
        --query recommendationId --output text)
  ok "id = $RID"
  for _ in $(seq 1 90); do
    st=$(aws bedrock-agentcore get-recommendation --recommendation-id "$RID" --query status --output text)
    case "$st" in PENDING|IN_PROGRESS) sleep 20 ;; *) break ;; esac
  done
  aws bedrock-agentcore get-recommendation --recommendation-id "$RID" --output json > "$CD/recommendation.result.json"
  if [[ "$st" != "COMPLETED" ]]; then
    msg=$(jq -r '.recommendationResult.systemPromptRecommendationResult.errorMessage // ""' "$CD/recommendation.result.json")
    if [[ "$msg" == *"prompt attack protection"* ]]; then
      warn "Recommendation 的 prompt 攻击防护把底稿判为不安全（实测：中文 v1『……让用户满意。』被拦，英文同义句与更长的中文 prompt 都能通过）"
      warn "处理：用英文底稿重试（BASE_PROMPT_OVERRIDE='…'），或改用 manual 模式人工撰写候选"
    fi
    die "推荐失败：$(jq -c '.recommendationResult' "$CD/recommendation.result.json")"
  fi
  jq -r '.recommendationResult.systemPromptRecommendationResult | .recommendedSystemPrompt // .systemPrompt // .text' \
     "$CD/recommendation.result.json" > "$CD/system_prompt.txt"
  jq -r '.recommendationResult.systemPromptRecommendationResult.explanation // ""' "$CD/recommendation.result.json" > "$CD/rationale.md"
else
  SRC="${3:?prompt 文本文件}"; [[ -f "$SRC" ]] || die "找不到 $SRC"
  cp "$SRC" "$CD/system_prompt.txt"
  [[ -f "$CD/rationale.md" ]] || echo "（人工撰写：在此写明针对的根因、机制、证据 trace id）" > "$CD/rationale.md"
fi

python3 - "$CD" "$CID" "$BASELINE_PROMPTS" <<'EOF'
import json,sys,copy,time
cd,cid,basefile=sys.argv[1:]
base=json.load(open(basefile)); text=open(f"{cd}/system_prompt.txt").read().strip()
assert text, "推荐结果为空"
d=copy.deepcopy(base); p=d["order_support"]
for m in p["messages"]:
    if m["role"]=="system": m["content"]=text
p["history"].append({"version":len(p["history"])+1,"versionId":cid,"timestamp":int(time.time()*1000),
    "commitMessage":f"training candidate {cid}","messages":copy.deepcopy(p["messages"]),
    "model":copy.deepcopy(p["model"]),"labels":["candidate"]})
# 不变量：除 system prompt 与新增 history 外逐键一致
chk=copy.deepcopy(d); chk["order_support"]["history"]=chk["order_support"]["history"][:-1]
for m in chk["order_support"]["messages"]:
    if m["role"]=="system": m["content"]=[x for x in base["order_support"]["messages"] if x["role"]=="system"][0]["content"]
assert json.dumps(chk,sort_keys=True)==json.dumps(base,sort_keys=True), "候选与 baseline 除 prompt 外有差异"
json.dump(d,open(f"{cd}/prompts.json","w"),ensure_ascii=False,indent=2)
print(f"  ✓ 候选 {cid}：system prompt {len(text)} 字（baseline {len([x for x in base['order_support']['messages'] if x['role']=='system'][0]['content'])} 字）")
EOF
echo "----- 候选 system prompt -----"; cat "$CD/system_prompt.txt"; echo
log "下一步：人工审阅 → 01_deploy.sh $CID $CD/prompts.json"
