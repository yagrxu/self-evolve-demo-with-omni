#!/usr/bin/env bash
# Step 3 —— 把第一波数据集注册成 AgentCore Dataset，并打一个不可变版本。
#
#   bash training/cloudwatch/scripts/03_dataset.sh control   # → CONTROL_DATASET_FILE
#   bash training/cloudwatch/scripts/03_dataset.sh confirm   # → CONFIRM_DATASET_FILE（确认集，封存）
#
# 数据集格式（AGENTCORE_EVALUATION_PREDEFINED_V1，实测）：
#   {scenario_id, turns:[{input, expected_response}], assertions:[str], expected_trajectory:[str], metadata:{}}
# expected_response 必须来自确定性的 oracle（这里是 agent/tools.py 的纯函数），
# 绝不能拿某次线上回答当标准答案 —— 那会把错误答案冻结成"正确"。
source "$(dirname "$0")/lib/common.sh"
ROLE="${1:?用法：03_dataset.sh control|confirm|confirm2}"
case "$ROLE" in
  control) FILE="$CONTROL_DATASET_FILE" ;;
  confirm) FILE="$CONFIRM_DATASET_FILE" ;;
  confirm2) FILE="$CONFIRM2_DATASET_FILE" ;;
  *) die "未知角色 $ROLE" ;;
esac
NAME="${RUN_ID}_${ROLE}"   # dataset 名：[a-zA-Z][a-zA-Z0-9_]{0,47}

existing=$(aws bedrock-agentcore-control list-datasets --query "datasets[?datasetName=='$NAME'].datasetId | [0]" --output text 2>/dev/null || echo None)
if [[ "$existing" != "None" && -n "$existing" ]]; then
  DID="$existing"; ok "已存在：$NAME → ${DID}（不重复创建）"
else
  log "创建 dataset ${NAME}（$(jq '.examples|length' "$FILE") 条，来自 ${FILE}）"
  jq -n --arg n "$NAME" --arg d "$(jq -r .description "$FILE")" --slurpfile f "$FILE" \
     '{datasetName:$n, description:$d, schemaType:"AGENTCORE_EVALUATION_PREDEFINED_V1",
       source:{inlineExamples:{examples:$f[0].examples}}}' > "$RUN_DIR/dataset-$ROLE.request.json"
  DID=$(aws bedrock-agentcore-control create-dataset --cli-input-json "file://$RUN_DIR/dataset-$ROLE.request.json" \
        --query datasetId --output text)
  ok "创建：$DID"
fi

for _ in $(seq 1 30); do
  st=$(aws bedrock-agentcore-control get-dataset --dataset-id "$DID" --query status --output text)
  [[ "$st" == "ACTIVE" ]] && break; sleep 5
done
[[ "$st" == "ACTIVE" ]] || die "dataset 状态 $st"

n=$(aws bedrock-agentcore-control get-dataset --dataset-id "$DID" --query exampleCount --output text)
[[ "$n" == "$(jq '.examples|length' "$FILE")" ]] || die "条数不符：云上 ${n}，文件 $(jq '.examples|length' "$FILE")"
ok "读回核对：$n 条"

# 不可变版本：后面的评估都引用这个版本号，数据集再改也不影响已做的结论
ver=$(aws bedrock-agentcore-control create-dataset-version --dataset-id "$DID" --query 'datasetVersion' --output text 2>/dev/null || echo "?")
ok "版本：$ver"

# 本地快照（后续脚本从这里读 ground truth，保证与云上版本一致）
aws bedrock-agentcore-control list-dataset-examples --dataset-id "$DID" --max-results 1000 --output json \
  | jq '{examples:[.examples[] | (.content // .)]}' > "$RUN_DIR/dataset-$ROLE.snapshot.json"
python3 - "$RUN_DIR/dataset-$ROLE.snapshot.json" "$FILE" <<'EOF'
import json,sys
a=json.load(open(sys.argv[1]))["examples"]; b=json.load(open(sys.argv[2]))["examples"]
ids=lambda xs:sorted(x.get("scenario_id") for x in xs)
assert ids(a)==ids(b), f"scenario_id 集合不一致：{set(ids(a))^set(ids(b))}"
print("  ✓ 云上 scenario_id 集合与文件一致")
EOF
jq -n --arg id "$DID" --arg n "$NAME" --arg v "$ver" --arg f "$FILE" --arg s "$(shasum -a 256 "$FILE"|cut -d' ' -f1)" \
  '{dataset_id:$id,name:$n,version:$v,file:$f,file_sha256:$s}' > "$RUN_DIR/dataset-$ROLE.json"
log "下一步：04_traffic.sh baseline control 1"
