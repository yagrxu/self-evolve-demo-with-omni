#!/usr/bin/env bash
# Step 0 —— 前置检查（只读）。任何一项不过就停，不要带病往下走。
#
#   bash training/cloudwatch/scripts/00_prereq.sh                 # 只检查
#   bash training/cloudwatch/scripts/00_prereq.sh --enable-tracing # 顺便开启 Transaction Search
source "$(dirname "$0")/lib/common.sh"

log "工具"
need aws; need jq; need python3; need node; need npx
v=$(aws --version 2>&1 | sed -E 's#aws-cli/([0-9.]+).*#\1#')
python3 - "$v" <<'EOF' || die "aws CLI $v 太旧：需要 ≥ 2.38（含 bedrock-agentcore start-batch-evaluation / datasets）"
import sys; a=tuple(map(int,sys.argv[1].split(".")[:2])); sys.exit(0 if a>=(2,38) else 1)
EOF
ok "aws CLI $v · jq · python3 · node"
aws bedrock-agentcore start-batch-evaluation help >/dev/null 2>&1 || die "CLI 没有 start-batch-evaluation"
aws bedrock-agentcore-control create-dataset help >/dev/null 2>&1 || die "CLI 没有 create-dataset"
ok "CLI 含 batch evaluation / dataset / recommendation 命令"

log "身份与 region"
aws sts get-caller-identity --query '{account:Account,arn:Arn}' --output json
[[ -n "$AWS_REGION" ]] || die "AWS_REGION 未设置"
ok "region = $AWS_REGION"

log "Transaction Search（OTel span → CloudWatch Logs aws/spans）"
dest=$(aws xray get-trace-segment-destination --query Destination --output text 2>/dev/null || echo NONE)
st=$(aws xray get-trace-segment-destination --query Status --output text 2>/dev/null || echo NONE)
if [[ "$dest" != "CloudWatchLogs" || "$st" != "ACTIVE" ]]; then
  if [[ "${1:-}" == "--enable-tracing" ]]; then
    warn "当前 $dest/$st —— 正在开启（一个 account+region 只需一次）"
    # 与官方文档一致（Get started with AgentCore Observability · Step 1）：
    # 先授权 X-Ray 把 span 写进 aws/spans 与 application-signals，再切换 trace 目的地
    acct=$(aws sts get-caller-identity --query Account --output text)
    part=$(aws sts get-caller-identity --query Arn --output text | cut -d: -f2)
    aws logs put-resource-policy --policy-name AgentCoreTransactionSearch --policy-document "$(jq -cn \
      --arg p "$part" --arg r "$AWS_REGION" --arg a "$acct" '{Version:"2012-10-17",Statement:[{
        Sid:"TransactionSearchXRayAccess",Effect:"Allow",Principal:{Service:"xray.amazonaws.com"},
        Action:"logs:PutLogEvents",
        Resource:["arn:\($p):logs:\($r):\($a):log-group:aws/spans:*","arn:\($p):logs:\($r):\($a):log-group:/aws/application-signals/data:*"],
        Condition:{ArnLike:{"aws:SourceArn":"arn:\($p):xray:\($r):\($a):*"},StringEquals:{"aws:SourceAccount":$a}}}]}')" >/dev/null
    ok "已写入 logs resource policy（允许 X-Ray 写 aws/spans）"
    aws xray update-trace-segment-destination --destination CloudWatchLogs >/dev/null
    aws xray update-indexing-rule --name Default \
      --rule "{\"Probabilistic\":{\"DesiredSamplingPercentage\":${INDEX_PCT:-100}}}" >/dev/null
    # 索引比例：1% 免费；评估需要完整 trace 时用 100%（INDEX_PCT 可调），注意成本
    ok "已开启。生效需几分钟，稍后重跑本脚本确认 ACTIVE"
  else
    die "Transaction Search 未开启（$dest/${st}）。加 --enable-tracing 重跑"
  fi
else
  ok "destination=$dest status=$st"
fi
aws xray get-indexing-rules --query 'IndexingRules[].{name:Name,pct:Rule.Probabilistic.DesiredSamplingPercentage}' --output text
aws logs describe-log-groups --log-group-name-prefix "$SPANS_LOG_GROUP" --query 'logGroups[0].logGroupName' --output text \
  | grep -q "$SPANS_LOG_GROUP" && ok "log group $SPANS_LOG_GROUP 存在" || warn "$SPANS_LOG_GROUP 还不存在（首个 span 落地后自动创建）"

log "Bedrock 模型可用性"
model=$(jq -r '.order_support.model.modelId' agent/prompts.json)
base=${model#us.}
aws bedrock list-foundation-models --query "modelSummaries[?modelId=='$base'].modelId" --output text | grep -q . \
  && ok "$model 在 $AWS_REGION 可用" || die "$model 在 $AWS_REGION 不可用"

log "数据集文件"
for f in "$CONTROL_DATASET_FILE" "$CONFIRM_DATASET_FILE"; do
  n=$(jq '.examples | length' "$f"); ok "${f}：$n 条"
done
log "通过。下一步：01_deploy.sh baseline"
