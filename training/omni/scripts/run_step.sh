#!/usr/bin/env bash
# Omni 流程的单步执行器：一次只跑一步，跑完就停，打印「做了什么 / 为什么 / 产物」，等人确认。
#
#   bash training/omni/scripts/run_step.sh 1                       # preflight
#   bash training/omni/scripts/run_step.sh 2 "Strands Agents" "资格判定错误；金额算错；不可退时没给替代路径"
#   export RUN=<上一步打印的 run_id>
#   bash training/omni/scripts/run_step.sh 3 … 11
#
# 前提：在 Kiro 里打开本仓库，并且 Amazon CloudWatch Omni 扩展为本目录运行（.omni/mcp-port 存在）。
# 云端 trace 查询需要 Kiro 带 AWS_REGION 启动：AWS_REGION=us-west-2 open -na Kiro --args "$(pwd)"
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"; cd "$REPO"
PY=.venv/bin/python; [[ -x $PY ]] || PY=python3
S=training/omni/scripts
STEP="${1:?步骤号 1–11（6b = A/A）}"; shift || true
need_run() { [[ -n "${RUN:-}" ]] || { echo "先 export RUN=<run_id>（步骤 2 打印的）"; exit 2; }; }
D() { echo ".omni/self-evolution/${RUN}"; }
banner() { printf '\n\033[1;36m== Step %s：%s ==\033[0m\n为什么：%s\n\n' "$1" "$2" "$3"; }
done_() { printf '\n\033[1;32m✓ Step %s 完成。\033[0m 产物：%s\n请审阅 / 截图，确认后再运行下一步。\n' "$1" "$2"; }

case "$STEP" in
1) banner 1 "Preflight" "确认凭证可签名、prompt 由 prompts.json 管、span 带版本和 hash —— 否则后面的结果无法归因"
   $PY $S/omni_steps.py preflight; done_ 1 "（终端输出）" ;;
2) banner 2 "全层状态快照" "记录改动前每一层的确切状态；不在这里决定改哪一层"
   $PY $S/omni_steps.py snapshot --agent "${1:?agent 名}" --complaint "${2:?quality complaint}"
   done_ 2 ".omni/self-evolution/<run>/manifest.json" ;;
3) need_run; banner 3 "拉取生产 trace（只读）" "用 Omni search_agent_traces 取最近 24h 该 agent 的全部 span"
   $PY scripts/se_harvest.py --run "$RUN"; done_ 3 "$(D)/traces/raw.jsonl" ;;
4) need_run; banner 4 "检查生产评估结果 + 冻结 replay 标尺" "生产标尺在 run 之前就定了；这里检查覆盖、值域、缺口，并把本地 replay 的标尺对齐冻结"
   $PY scripts/se_step4_check.py --run "$RUN"; $PY $S/omni_steps.py ruler --run "$RUN"
   done_ 4 "$(D)/production_eval/check.json · $(D)/evaluators.json" ;;
5) need_run; banner 5 "切分 control / holdout，封存 fresh" "诊断和候选只看 control；切分是 scenario_id 的纯函数"
   $PY scripts/se_step6.py --run "$RUN"; cp datasets/fresh.json "$(D)/datasets/fresh.json"
   done_ 5 "$(D)/datasets/split.json" ;;
6) need_run; banner 6 "基线重复采样 + 按层诊断" "生产每个 scenario 只有 1 个样本，分不清系统性 vs 偶发；不变的 baseline 在 control 上跑 3 遍"
   caffeinate -dimsu $PY -u scripts/replay.py --run "$RUN" --set control --repetitions 3 --variants baseline
   mkdir -p "$(D)/diagnosis/runs"; mv "$(D)/runs/baseline" "$(D)/diagnosis/runs/baseline"; mv "$(D)/replay-control.json" "$(D)/diagnosis/"
   rmdir "$(D)/runs" 2>/dev/null || true
   $PY scripts/se_diagnose.py --run "$RUN"
   done_ 6 "$(D)/diagnosis/summary.json（人工逐条读判词 → attribution.json，由人选定本轮改哪一层）" ;;
6b) need_run; banner 6b "A/A 校准 + 登记门禁" "不知道「什么都没改时 Δ 抖多少」，门槛就是拍脑袋；登记必须早于任何候选"
   caffeinate -dimsu $PY -u scripts/replay.py --run "$RUN" --set control --repetitions 3 --variants baseline
   mkdir -p "$(D)/runs-aa"; mv "$(D)/runs/baseline" "$(D)/runs-aa/baseline"; mv "$(D)/replay-control.json" "$(D)/replay-aa.json"
   rmdir "$(D)/runs" 2>/dev/null || true
   $PY $S/gates_register.py --run "$RUN" --stage S0
   done_ 6b "$(D)/gates.registered.S0.json" ;;
7) need_run; banner 7 "生成候选（只改选定的那一层）" "每个候选是完整独立的 prompts.json；除 prompt 外逐键一致（脚本断言）"
   $PY $S/omni_steps.py candidate --run "$RUN" "$@"; done_ 7 "$(D)/candidates/<id>/" ;;
8) need_run; banner 8 "paired replay（control）" "baseline 与候选同时段、同顺序、同题目；每格核对 trace 上的 prompt 版本"
   caffeinate -dimsu $PY -u scripts/replay.py --run "$RUN" --set control --repetitions 3 | tee "$(D)/replay-control.log"
   done_ 8 "$(D)/runs/<arm>/invocations.jsonl" ;;
9) need_run; banner 9 "门禁（S0）" "用事先登记的门槛做配对 bootstrap、逐维回退、成本、safety —— 全部是脚本算术"
   $PY scripts/se_gates.py --run "$RUN" --set control --registered "$(D)/gates.registered.S0.json" | tee "$(D)/gates-control.log"
   done_ 9 "$(D)/gates-control.json" ;;
10) need_run; LEADER="${1:?领先候选 id}"; banner 10 "确认集（S1：fresh，从未被看过）" "先登记 S1 门禁再跑；只测一个候选；只跑一次"
   $PY $S/gates_register.py --run "$RUN" --stage S1 || true
   caffeinate -dimsu $PY -u scripts/replay.py --run "$RUN" --set fresh --repetitions 3 --variants "baseline,$LEADER"
   $PY scripts/se_gates.py --run "$RUN" --set fresh --registered "$(D)/gates.registered.S1.json" | tee "$(D)/gates-fresh.log"
   done_ 10 "$(D)/gates-fresh.json" ;;
11) need_run; banner 11 "记录决定 / 本地写回" "防篡改校验 + 其余层未变校验；只写本地，不部署"
   $PY $S/omni_steps.py decide --run "$RUN" --winner "${1:?候选 id}" --gates "${2:-gates-fresh.json}" ${3:+--writeback}
   done_ 11 "$(D)/decision.json" ;;
*) echo "未知步骤 $STEP"; exit 2 ;;
esac
