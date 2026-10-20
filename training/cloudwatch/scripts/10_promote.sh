#!/usr/bin/env bash
# Step 10 —— 上线 / 回滚。**对外可见的变更**：必须显式 CONFIRM=yes，并且门禁结论文件必须是 PASS。
#
#   CONFIRM=yes bash training/cloudwatch/scripts/10_promote.sh c1 S1     # 用 c1 的 prompts 重新部署 baseline runtime
#   CONFIRM=yes bash training/cloudwatch/scripts/10_promote.sh --rollback  # 回到上一次的 prompts
#
# 做法：把胜者的 prompts.json 打进 baseline 的包，用 CDK 重新部署 baseline stack。
# runtime 会生成新版本，endpoint 指向新版本；上一个版本的 prompts 备份在 runs/<RUN_ID>/promote/。
# 这一步之前，按分阶段门禁应已完成 S2（云上灰度）—— 见 workflow.md。
source "$(dirname "$0")/lib/common.sh"
PD="$RUN_DIR/promote"; mkdir -p "$PD"

if [[ "${1:-}" == "--rollback" ]]; then
  PREV=$(ls -t "$PD"/prompts.before.*.json 2>/dev/null | head -1) || true
  [[ -n "$PREV" ]] || die "没有可回滚的备份"
  [[ "${CONFIRM:-}" == "yes" ]] || die "回滚会重新部署 baseline runtime。确认请加 CONFIRM=yes"
  log "回滚到 $PREV"
  bash "$(dirname "$0")/01_deploy.sh" baseline "$PREV"
  exit 0
fi

CID="${1:?候选 id}"; STAGE="${2:?最后通过的阶段，如 S1 或 S2}"
G="$RUN_DIR/gates-$STAGE-$CID-$( [[ $STAGE == S0 ]] && echo control || echo confirm ).json"
[[ -f "$G" ]] || die "找不到门禁结论 $G"
v=$(jq -r .verdict "$G"); [[ "$v" == "PASS" ]] || die "门禁结论是 ${v}，不能上线"
[[ "$STAGE" == "S2" ]] || warn "只通过了 ${STAGE}（本地）。按分阶段门禁，上线前应先过 S2 云上灰度"
NEW="$RUN_DIR/candidates/$CID/prompts.json"; [[ -f "$NEW" ]] || die "缺 $NEW"

echo "将要做的事："
echo "  · 重新部署 $(stack_name baseline)，包内 prompts.json ← $NEW"
echo "  · 版本 $(jq -r '.order_support.history[-1].versionId' "$NEW")（当前线上：$(jq -r '.order_support.history[-1].versionId' agent/prompts.json)）"
[[ "${CONFIRM:-}" == "yes" ]] || die "这是线上变更。确认请加 CONFIRM=yes"

ts=$(date +%Y%m%dT%H%M%S)
cp agent/prompts.json "$PD/prompts.before.$ts.json"
bash "$(dirname "$0")/01_deploy.sh" baseline "$NEW"
cp "$NEW" agent/prompts.json   # 源码同步：否则下次打包会把旧 prompt 部署回去（需人工 git commit）
jq -n --arg c "$CID" --arg s "$STAGE" --arg g "$G" --arg t "$ts" '{promoted:$c,stage:$s,gates:$g,at:$t}' > "$PD/promote.$ts.json"
ok "已上线 ${CID}。回滚：CONFIRM=yes bash $0 --rollback"
