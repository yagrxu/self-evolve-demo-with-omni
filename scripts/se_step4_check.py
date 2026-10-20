#!/usr/bin/env python3
"""Step 4（修订版）—— 检查生产评估结果，而不是在这里挑选 evaluator。

标尺在 run 之前就定好了：云上 online evaluation config（CDK 部署于 2026-09-18）
在流量进来时已经自动打分。这一步只做**检查**：
  1. 覆盖率：每个 evaluator 覆盖了多少个 session
  2. 值域核对：实际出现的分值是否落在 get_evaluator 声明的档位里
  3. 不及格条数（按 evaluator、按 session）
  4. 标尺缺口：云上缺什么、本地 replay 跑不了什么

只读：logs:FilterLogEvents + manage_evaluations(get_evaluator)。

    python scripts/se_step4_check.py --run <run_id>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_cloud_scores import (EVAL_EXPL_ATTR, discover_eval_log_group, fetch_events,  # noqa: E402
                                find_scores, parse_records, record_session)
from omni_client import omni  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
# 及格线（原始分）。与 evaluators/semantics.json 及本 run 的 replay 标尺一致：
# PolicyGrounding ≥2「基本有依据」；Helpfulness ≥0.67「Somewhat Helpful」；其余 ≥0.7
PASS_RAW = {"selfevolve_demo_PolicyGrounding": 2, "Builtin.Helpfulness": 0.67}
PASS_DEFAULT = 0.7
SAFETY_EXPECTED = ["Builtin.Harmfulness", "Builtin.Stereotyping",
                   "ThirdParty.DeepEval.PIILeakage", "ThirdParty.AutoEval.Security"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = REPO / ".omni" / "self-evolution" / args.run
    m = json.loads((run_dir / "manifest.json").read_text())

    sess_ids = set()
    windows = []
    for r in m["traffic_runs"]:
        tm = json.loads((REPO / "runs" / r / "manifest.json").read_text())
        sess_ids |= set(tm["session_to_scenario"])
        windows.append((tm["window_start_epoch_s"], tm["window_end_epoch_s"]))
    s2s = {}
    for r in m["traffic_runs"]:
        s2s.update(json.loads((REPO / "runs" / r / "manifest.json").read_text())["session_to_scenario"])

    logs = boto3.client("logs", region_name=m["environment"]["region"])
    lg = discover_eval_log_group(logs, m["environment"]["region"])
    start = min(w[0] for w in windows) * 1000
    end = (max(w[1] for w in windows) + 4 * 3600) * 1000
    recs = parse_records(fetch_events(logs, lg, start, end))

    cfg = boto3.client("bedrock-agentcore-control", region_name=m["environment"]["region"])
    oec = cfg.list_online_evaluation_configs()["onlineEvaluationConfigs"][0]

    scores: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in recs:
        s = record_session(r)
        if s not in sess_ids:
            continue
        expl = (r["raw"].get("attributes") or {}).get(EVAL_EXPL_ATTR) or ""
        for ev_id, raw in find_scores(r):
            scores[s][ev_id].append({"raw": raw, "trace_id": r["raw"].get("traceId"),
                                     "span_id": r["raw"].get("spanId"), "explanation": expl[:1500]})

    evaluators = sorted({e for d in scores.values() for e in d})
    lst = omni("manage_evaluations", {"action": "list_evaluators", "project_path": "."})["evaluators"]
    by_name = {e["name"]: e for e in lst}

    report = {"online_eval_config": {k: str(oec[k]) for k in
                                     ("onlineEvaluationConfigName", "onlineEvaluationConfigId",
                                      "status", "executionStatus", "createdAt")},
              "log_group": lg, "sessions_expected": len(sess_ids),
              "sessions_scored": len(scores), "evaluators": {}}
    for ev in evaluators:
        meta = by_name.get(ev) or by_name.get(ev.split("/")[-1]) or {}
        g = omni("manage_evaluations", {"action": "get_evaluator",
                                        "evaluator_id": meta.get("id", ev)})
        scale = [x["value"] for x in ((g.get("ratingScale") or {}).get("numerical") or [])]
        lo, hi = (min(scale), max(scale)) if scale else (0, 1)
        vals = [o["raw"] for d in scores.values() for o in d.get(ev, [])]
        observed = sorted({round(v, 2) for v in vals})
        off_scale = [v for v in observed if scale and not any(abs(v - s) < 0.011 for s in scale)]
        thr = PASS_RAW.get(ev, PASS_DEFAULT)
        fail_sessions = sorted(s for s, d in scores.items() if ev in d and min(
            o["raw"] for o in d[ev]) < thr - 1e-9)
        report["evaluators"][ev] = {
            "level": g.get("level") or meta.get("level"),
            "declared_scale": scale or "无 ratingScale",
            "observed_values": observed, "off_scale_values": off_scale,
            "observations": len(vals), "pass_threshold_raw": thr,
            "sessions_covered": sum(1 for d in scores.values() if ev in d),
            "failing_sessions": len(fail_sessions),
            "failing_scenarios": sorted(s2s[s] for s in fail_sessions)}
    present = set(evaluators)
    report["gaps"] = {
        "missing_safety_in_production": [e for e in SAFETY_EXPECTED if e not in present],
        "not_runnable_in_local_replay": sorted(
            e for e in evaluators if report["evaluators"][e]["level"] in ("TOOL_CALL", "SESSION")),
        "correctness_reference_free_online": "云上 Correctness 没有 expected_response 可比；"
                                             "本地 replay 可带 oracle —— 两边同名不同义"}
    (run_dir / "production_eval").mkdir(exist_ok=True)
    (run_dir / "production_eval" / "scores.json").write_text(json.dumps(
        {s: {"scenario_id": s2s[s], "scores": d} for s, d in scores.items()},
        ensure_ascii=False, indent=2))
    (run_dir / "production_eval" / "check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))

    print(f"online eval config：{oec['onlineEvaluationConfigName']}  {oec['status']}/"
          f"{oec['executionStatus']}  创建于 {oec['createdAt']}")
    print(f"结果 log group：{lg}")
    print(f"session 覆盖：{len(scores)} / {len(sess_ids)}\n")
    print(f"{'evaluator':36s} {'层级':9s} {'覆盖':>5s} {'观测':>5s} {'不及格session':>12s}  值域核对")
    for ev, r in report["evaluators"].items():
        ok = "✅" if not r["off_scale_values"] else f"❌ 越界 {r['off_scale_values']}"
        print(f"{ev:36s} {str(r['level']):9s} {r['sessions_covered']:>5d} {r['observations']:>5d} "
              f"{r['failing_sessions']:>12d}  及格≥{r['pass_threshold_raw']}  {ok}  声明 {r['declared_scale']} 实测 {r['observed_values']}")
    print("\n标尺缺口：")
    for k, v in report["gaps"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
