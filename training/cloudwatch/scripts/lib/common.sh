#!/usr/bin/env bash
# 所有步骤脚本共用的函数。只依赖 aws CLI v2、jq、python3（标准库）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
TRAINING_DIR="$REPO_ROOT/training/cloudwatch"
# shellcheck source=../../config.env
source "$TRAINING_DIR/config.env"
cd "$REPO_ROOT"

RUN_DIR="$TRAINING_DIR/runs/$RUN_ID"
mkdir -p "$RUN_DIR"

log()  { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*" >&2; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*" >&2; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }

now_ms() { python3 -c 'import time; print(int(time.time()*1000))'; }
iso_utc() { python3 -c "import datetime,sys; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"; }

# arm 名 → runtime 名 / stack 名。baseline 不带后缀；候选用 AGENT_RUNTIME_SUFFIX。
runtime_name() { [[ "$1" == "baseline" ]] && echo "$RUNTIME_BASE" || echo "${RUNTIME_BASE}_$1"; }
stack_name()   { [[ "$1" == "baseline" ]] && echo "${STACK_PREFIX}-agent" || echo "${STACK_PREFIX}-agent-${1//_/-}"; }
service_name() { echo "$(runtime_name "$1").${ENDPOINT_NAME}"; }

stack_output() {  # stack_output <stack> <OutputKey>
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

runtime_arn() { stack_output "$(stack_name "$1")" RuntimeArn; }
runtime_id()  { stack_output "$(stack_name "$1")" RuntimeId; }
runtime_log_group() { echo "/aws/bedrock-agentcore/runtimes/$(runtime_id "$1")-${ENDPOINT_NAME}"; }

# session id 把 run / arm / scenario / batch 编进去 —— 后面所有对齐都靠它，绝不靠位置。
# AgentCore 要求 runtimeSessionId ≥ 33 字符。
session_id() {  # session_id <arm> <scenario_id> <batch>
  local s="cwt-${RUN_ID}-$1-$2-b$3"
  while (( ${#s} < 33 )); do s="${s}-0"; done
  echo "$s"
}

wait_batch_eval() {  # wait_batch_eval <id> → 打印最终状态
  local id="$1" st
  for _ in $(seq 1 120); do
    st=$(aws bedrock-agentcore get-batch-evaluation --batch-evaluation-id "$id" --query status --output text)
    case "$st" in PENDING|IN_PROGRESS) sleep 20 ;; *) echo "$st"; return 0 ;; esac
  done
  echo "TIMEOUT"
}
