#!/usr/bin/env python3
"""Phase 4 —— 把云上表现差的 trace 导出到本地，构建 Phase 5 的合格样本集。

这个脚本就是 ``omni-self-evolution`` SKILL.md 的 **Step 3 ~ Step 6** 的实现：

  Step 3  只读拉取生产 trace（这里走 boto3 直读 CloudWatch Logs，见下）
  Step 4  evaluator 集已在 Phase 2 冻结，直接读 evaluators/semantics.json
  Step 5  给 baseline 打分（复用云上在线评估的结果）+ 归纳失败模式
  Step 6  构建合格样本集并切分 control / holdout

为什么走 boto3 而不是 ``search_agent_traces``
---------------------------------------------
Omni 的云端 endpoint 目前不可用（决策点 R8：扩展宿主进程 env 里没有 AWS_REGION，
``mcp-proxy.js`` 又不转发 env）。评估结果本来就落在
``/aws/bedrock-agentcore/evaluations/results/<configId>``，直读是最短路径。

**这条通路不是降级方案**：它不依赖 IDE 会话，能在 CI 里跑。
代价是拿不到 ``manage_annotations`` 的云端标注能力 —— 失败模式因此落盘成
``failure_modes.json``，而不是打在云端 trace 上。R8 解决后可以补标注。

只读保证（SKILL.md Invariant 1）
--------------------------------
本脚本只调用 ``logs:FilterLogEvents`` 这类读操作，**不写任何云端资源**。

用法：
    python scripts/export_bad_traces.py --run traffic-prod_sim-0be7e8bc
    python scripts/export_bad_traces.py --run <run> --holdout-fraction 0.30
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_cloud_scores import (  # noqa: E402
    EVAL_EXPL_ATTR,
    EVAL_NAME_ATTR,
    canonical,
    discover_eval_log_group,
    fetch_events,
    find_scores,
    logical_name,
    parse_records,
    record_session,
)

REPO = Path(__file__).resolve().parent.parent

# ── SKILL.md Step 6 的门槛。改动这些值等于改变结论的可信度，必须同步改 SKILL.md ──
MIN_ELIGIBLE_EXAMPLES = 12   # 低于 12，30% holdout 不足 4 条，单条样本能让均值动 >25%
MIN_CONTROL_EXAMPLES = 5
MIN_HOLDOUT_EXAMPLES = 5
HOLDOUT_FRACTION = 0.30


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def stable_bucket(scenario_id: str) -> float:
    """把 scenario_id 稳定映射到 [0,1)。

    **必须是纯函数**（SKILL.md Invariant 5）：重跑本脚本必须得到完全相同的切分。
    随机或带运行种子的切分可以反复重摇到候选通过，而报告里看不出任何异常。

    用 blake2b 而不是内置 hash()：内置 str hash 受 PYTHONHASHSEED 随机化影响，
    跨进程不稳定 —— 那正是 Phase 1.4 修过的同一类 bug。
    """
    h = hashlib.blake2b(scenario_id.encode("utf-8"), digest_size=8).hexdigest()
    return int(h, 16) / float(1 << 64)


def classify_failures(scenario: str, scores: dict[str, list[float]], semantics: dict,
                      explanations: dict[str, list[str]]) -> list[dict]:
    """判断这条 scenario 在哪些维度上不及格，返回失败项列表。"""
    out = []
    for logical, sem in semantics.items():
        vals = scores.get(logical) or []
        if not vals:
            continue
        mean = statistics.fmean(vals)
        pass_canon = canonical(float(sem["pass_threshold"]), sem)
        if mean < pass_canon:
            out.append({
                "evaluator": logical,
                "mean": round(mean, 4),
                "pass_threshold_canonical": round(pass_canon, 4),
                "n": len(vals),
                "explanation_sample": (explanations.get(logical) or [""])[0][:400],
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, action="append",
                    help="runs/ 下的流量 run 目录名；可重复传以合并样本池")
    ap.add_argument("--holdout-fraction", type=float, default=HOLDOUT_FRACTION)
    ap.add_argument("--slack", type=int, default=10800,
                    help="查询窗口在流量窗口后延展多少秒（在线评估异步，会晚于流量）")
    args = ap.parse_args()

    # 可以合并多个流量 run 的样本池。
    #
    # 为什么需要这个：单个 dataset 的 scenario 数量决定了切分后 holdout 的规模。
    # prod_sim 只有 20 个 scenario，30% 稳定哈希切出 3 条（期望 6，n=20 时的正常波动），
    # 低于 SKILL.md Step 6 的 holdout ≥5 门槛 → NO_DECISION。
    #
    # 按 SKILL.md，此时**不得**降门槛、不得重切、不得合成样本 ——
    # 唯一正当的做法是**补真实流量**。而对同一个 dataset 再发一轮只会得到同样的
    # scenario_id 集合，切分完全不变；必须扩大 **scenario 池**本身。
    # 所以支持把多个 dataset 的云上流量合并进同一个池。
    #
    # ⚠️ `verify` 永远不进这个池 —— 它是 Phase 6 的最终验证集，见下方断言。
    mfs = [load_json(REPO / "runs" / r / "manifest.json") for r in args.run]
    sem_file = load_json(REPO / "evaluators" / "semantics.json")
    semantics = {k: v for k, v in sem_file["quality_evaluators"].items() if not k.startswith("_")}

    datasets_used = sorted({m["dataset"] for m in mfs})
    if "verify" in datasets_used:
        raise SystemExit(
            "拒绝执行：`verify` 是 Phase 6 的 held-out 验证集，不能进入 Phase 5 的样本池。\n"
            "一旦它被看过，Phase 6 就只能衡量记忆而不是改进。")

    ex_by_sid: dict[str, dict] = {}
    for ds_name in datasets_used:
        for e in load_json(REPO / "datasets" / f"{ds_name}.json")["examples"]:
            ex_by_sid[e["scenario_id"]] = e

    region = mfs[0].get("region", "us-west-2")
    s2s: dict[str, str] = {}
    for m in mfs:
        s2s.update(m["session_to_scenario"])
    logs = boto3.client("logs", region_name=region)

    print(f"runs={args.run}  datasets={datasets_used}  region={region}  session={len(s2s)}")

    # ── Step 3：只读拉取 ──────────────────────────────────────────────────
    log_group = discover_eval_log_group(logs, region)
    if not log_group:
        raise SystemExit("找不到在线评估结果 log group —— eval stack 部署了吗？")
    start_ms = min(int(m["window_start_epoch_s"]) for m in mfs) * 1000
    events = fetch_events(logs, log_group, start_ms, int(time.time() * 1000))
    records = parse_records(events)
    print(f"拉到 {len(events)} 条事件 / {len(records)} 条评分记录")

    # ── Step 5：按 scenario 归集分数与判词 ────────────────────────────────
    scores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    expl: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    trace_ids: dict[str, set[str]] = defaultdict(set)

    for rec in records:
        sid = record_session(rec)
        if sid is None or sid not in s2s:
            continue
        scenario = s2s[sid]
        attrs = rec["raw"].get("attributes") or {}
        tid = rec["raw"].get("traceId")
        if tid:
            trace_ids[scenario].add(tid)
        for ev_id, raw in find_scores(rec):
            logical = logical_name(ev_id, semantics)
            if logical is None:
                continue
            try:
                scores[scenario][logical].append(canonical(raw, semantics[logical]))
            except (TypeError, ValueError):
                continue
            e = attrs.get(EVAL_EXPL_ATTR)
            if isinstance(e, str) and e:
                expl[scenario][logical].append(e)

    covered = sorted(scores)
    print(f"覆盖 {len(covered)}/{len(s2s)} 个 scenario")
    if len(covered) < len(s2s):
        print(f"  ⚠️  {len(s2s)-len(covered)} 条没有云端评分 —— 它们**不会**进入合格样本集。"
              f"未评分不等于表现差，把它们当差样本会污染整个实验。")

    # ── Step 5：归纳失败模式 ─────────────────────────────────────────────
    per_scenario_failures = {
        s: classify_failures(s, scores[s], semantics, expl[s]) for s in covered
    }
    modes: dict[str, dict] = defaultdict(lambda: {"scenarios": [], "archetypes": set()})
    for s, fails in per_scenario_failures.items():
        for f in fails:
            m = modes[f["evaluator"]]
            m["scenarios"].append(s)
            arch = ex_by_sid.get(s, {}).get("metadata", {}).get("archetype")
            if arch:
                m["archetypes"].add(arch)

    failure_modes = [
        {
            "name": f"low_{ev.split('.')[-1]}",
            "evaluator": ev,
            "affected_count": len(v["scenarios"]),
            "affected_scenarios": sorted(v["scenarios"]),
            "archetypes": sorted(v["archetypes"]),
            "example_explanation": next(
                (f["explanation_sample"] for s in v["scenarios"]
                 for f in per_scenario_failures[s] if f["evaluator"] == ev
                 and f["explanation_sample"]), ""),
        }
        for ev, v in sorted(modes.items(), key=lambda kv: -len(kv[1]["scenarios"]))
    ]

    # SKILL.md Step 5：没有失败模式覆盖到 ≥2 条时停止 ——
    # 单条样本的抱怨无法与噪声区分，任何下游门禁都判不出来。
    if not failure_modes or failure_modes[0]["affected_count"] < 2:
        print("\n✗ 没有任何失败模式覆盖 ≥2 条 scenario。")
        print("  按 SKILL.md Step 5 应以 NO_DECISION 停止 —— 单条抱怨无法与噪声区分。")

    # ── Step 6：构建合格样本集 + 稳定切分 ────────────────────────────────
    # 合格 = 有云端评分 + 有 trace + dataset 里有对应的 ground truth。
    eligible = []
    for s in covered:
        ex = ex_by_sid.get(s)
        if ex is None or not trace_ids.get(s):
            continue
        rec = {
            **json.loads(json.dumps(ex)),
            "metadata": {
                **ex["metadata"],
                "source_trace_ids": sorted(trace_ids[s]),
                "source_run": args.run,
                "cloud_scores": {k: round(statistics.fmean(v), 4)
                                 for k, v in scores[s].items() if v},
                "failed_evaluators": [f["evaluator"] for f in per_scenario_failures[s]],
            },
        }
        eligible.append(rec)

    # 切分是 scenario_id 的纯函数 —— 与候选、与运行时刻无关
    holdout = [e for e in eligible if stable_bucket(e["scenario_id"]) < args.holdout_fraction]
    control = [e for e in eligible if stable_bucket(e["scenario_id"]) >= args.holdout_fraction]

    run_id = "+".join(f"{m['dataset']}-{m['run_id']}" for m in mfs)
    out_dir = REPO / ".omni" / "self-evolution" / run_id
    (out_dir / "datasets").mkdir(parents=True, exist_ok=True)

    for name, data in (("eligible", eligible), ("control", control), ("holdout", holdout)):
        (out_dir / "datasets" / f"{name}.json").write_text(
            json.dumps({"name": name, "count": len(data), "examples": data},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "failure_modes.json").write_text(
        json.dumps(failure_modes, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 门槛检查：不达标就如实报 NO_DECISION，绝不放宽 ────────────────────
    blockers = []
    if len(eligible) < MIN_ELIGIBLE_EXAMPLES:
        blockers.append(f"合格样本 {len(eligible)} < {MIN_ELIGIBLE_EXAMPLES}")
    if len(control) < MIN_CONTROL_EXAMPLES:
        blockers.append(f"control {len(control)} < {MIN_CONTROL_EXAMPLES}")
    if len(holdout) < MIN_HOLDOUT_EXAMPLES:
        blockers.append(f"holdout {len(holdout)} < {MIN_HOLDOUT_EXAMPLES}")
    verdict = "READY_FOR_PHASE_5" if not blockers else "NO_DECISION"

    print(f"\n合格样本 {len(eligible)}  →  control {len(control)} / holdout {len(holdout)}")
    print(f"判定：{verdict}")
    for b in blockers:
        print(f"  ✗ {b}")

    # ── 报告 ─────────────────────────────────────────────────────────────
    lines: list[str] = []
    a = lines.append
    a("# Phase 4 — 从云端导出差 trace，构建 Phase 5 样本集\n")
    a("- 来源流量 run：" + ", ".join(f"`{r}`" for r in args.run)
      + f"（dataset {', '.join('`'+d+'`' for d in datasets_used)}）")
    a(f"- 产物目录：`.omni/self-evolution/{run_id}/`（不入版本库 —— trace 可能含客户 payload）")
    a(f"- 生成时间：{datetime.now(timezone.utc).isoformat()}")
    a("- 对应 SKILL.md：**Step 3 ~ Step 6**")
    a(f"- `verify` 未参与（Phase 6 专用 held-out）：✅")
    a(f"- **判定：`{verdict}`**\n")

    a("## 样本集\n")
    a("| 集合 | 条数 | 门槛 | 结果 |")
    a("|---|---:|---:|---|")
    a(f"| eligible | {len(eligible)} | ≥{MIN_ELIGIBLE_EXAMPLES} | "
      f"{'✅' if len(eligible)>=MIN_ELIGIBLE_EXAMPLES else '❌'} |")
    a(f"| control | {len(control)} | ≥{MIN_CONTROL_EXAMPLES} | "
      f"{'✅' if len(control)>=MIN_CONTROL_EXAMPLES else '❌'} |")
    a(f"| holdout | {len(holdout)} | ≥{MIN_HOLDOUT_EXAMPLES} | "
      f"{'✅' if len(holdout)>=MIN_HOLDOUT_EXAMPLES else '❌'} |")
    a("")
    a(f"> 切分是 `scenario_id` 的 **blake2b 稳定哈希**（holdout = 哈希落在最低 "
      f"{args.holdout_fraction:.0%}），是纯函数 —— 重跑得到完全相同的切分。")
    a("> 这不是洁癖：随机或带运行种子的切分可以反复重摇到候选通过，"
      "而最终报告里看不出任何异常（SKILL.md Invariant 5）。\n")
    a(f"holdout：{', '.join('`'+e['scenario_id']+'`' for e in holdout) or '（空）'}\n")

    a("## 失败模式（按影响条数排序）\n")
    if failure_modes:
        a("| 失败模式 | evaluator | 影响条数 | 涉及原型 |")
        a("|---|---|---:|---|")
        for m in failure_modes:
            a(f"| `{m['name']}` | `{m['evaluator']}` | {m['affected_count']} | "
              f"{', '.join(m['archetypes'][:4])}{'…' if len(m['archetypes'])>4 else ''} |")
        a("")
        top = failure_modes[0]
        if top["example_explanation"]:
            a(f"**`{top['name']}` 的判词样例**（来自云端 evaluator，非我们撰写）：\n")
            a("> " + top["example_explanation"].replace("\n", "\n> ") + "\n")
    else:
        a("（没有任何维度不及格 —— 这本身是个需要解释的结果）\n")

    a("## 逐场景失败维度\n")
    a("| scenario | 原型 | 不及格的 evaluator |")
    a("|---|---|---|")
    for s in covered:
        fails = per_scenario_failures[s]
        arch = ex_by_sid.get(s, {}).get("metadata", {}).get("archetype", "?")
        names = ", ".join(f"`{f['evaluator'].split('.')[-1]}` ({f['mean']:.2f})" for f in fails)
        a(f"| `{s}` | {arch} | {names or '—'} |")
    a("")

    a("## 下一步\n")
    if verdict == "READY_FOR_PHASE_5":
        a("Phase 5：按 SKILL.md Step 7 生成 prompt-only 候选，Step 8 在 control 集上做 "
          "paired replay，Step 9 过统计门禁，Step 10 在 holdout 上确认。")
        a("")
        a("**候选必须针对上面排第一的失败模式**，且每个候选只改一件事 —— "
          "一次改四处只能告诉你这一揽子有效，无法约减成最小安全变更。")
    else:
        a(f"**不进入 Phase 5。** 判定 `NO_DECISION`：{'；'.join(blockers)}。")
        a("")
        a("按 SKILL.md Step 6，此时**不得**降低门槛或合成样本 —— "
          "那会把一次诚实的「证据不足」变成假阳性。正确做法是补更多真实流量。")

    report = REPO / "reports" / "03-bad-traces.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ 报告：{report.relative_to(REPO)}")
    print(f"✓ 样本集：.omni/self-evolution/{run_id}/datasets/")
    return 0 if verdict == "READY_FOR_PHASE_5" else 2


if __name__ == "__main__":
    sys.exit(main())
