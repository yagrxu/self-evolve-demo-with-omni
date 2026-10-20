#!/usr/bin/env python3
"""SKILL.md Step 5 —— 用冻结的 evaluator 给生产 trace 打基线分，归纳失败模式。

    python scripts/se_step5.py --run <run_id>

产物：baseline_scores.json（逐 trace 分数 + 判词）、failure_modes.json。
对话里只报每个失败模式的条数（SKILL.md：不要把分数表抄进对话）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from se_eval import load_frozen, score_targets  # noqa: E402
from se_targets import from_spans, strip_private  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def scenario_of(session_id: str) -> tuple[str, str]:
    # selfevolve-<dataset>-<scenario_id>-<traffic_run_id>
    parts = session_id.split("-")
    return parts[1], parts[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = REPO / ".omni" / "self-evolution" / args.run
    frozen = load_frozen(run_dir)

    examples = {}
    for ds in ("prod_sim", "dev_local"):
        for e in json.loads((REPO / "datasets" / f"{ds}.json").read_text())["examples"]:
            examples[e["scenario_id"]] = e

    rows = [json.loads(l) for l in (run_dir / "traces" / "raw.jsonl").read_text().splitlines()]
    targets, meta = [], {}
    for r in rows:
        ds, sid = scenario_of(r["session_id"])
        if ds == "verify":
            raise SystemExit("verify 不得进入样本池")
        ex = examples.get(sid)
        if ex is None:
            continue
        tid = r["trace_ids"][0]
        t = from_spans(r["spans"], tid, ex)
        if not t["output"]:
            continue
        targets.append(strip_private(t))
        meta[tid] = {"scenario_id": sid, "dataset": ds, "session_id": r["session_id"],
                     "archetype": ex["metadata"]["archetype"],
                     "tools": [c["name"] for c in t["toolCalls"]]}
    print(f"可评分 trace：{len(targets)} / {len(rows)}")

    scores = score_targets(targets, frozen)
    out = {tid: {**meta[tid], "scores": scores.get(tid, {})} for tid in meta}
    (run_dir / "baseline_scores.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))

    # 失败模式：按「哪个质量维度低于冻结及格线」归类，并按原型细分
    modes: dict[str, dict] = defaultdict(lambda: {"traces": [], "archetypes": set(),
                                                   "examples": []})
    for tid, rec in out.items():
        for lg, s in rec["scores"].items():
            sem = frozen["evaluators"][lg]
            if sem["role"] == "safety":
                continue
            if s["raw"] < sem["pass_threshold"]:
                m = modes[lg]
                m["traces"].append(tid)
                m["archetypes"].add(rec["archetype"])
                if len(m["examples"]) < 3:
                    m["examples"].append({"trace_id": tid, "scenario_id": rec["scenario_id"],
                                          "explanation": s["explanation"][:600]})
    safety_fail = {lg: sum(1 for r in out.values()
                           if lg in r["scores"] and r["scores"][lg]["raw"]
                           < frozen["evaluators"][lg]["pass_threshold"])
                   for lg, sem in frozen["evaluators"].items() if sem["role"] == "safety"}
    fm = [{"name": f"low_{lg.split('.')[-1]}", "evaluator": lg,
           "depresses": [lg], "count": len(v["traces"]),
           "archetypes": sorted(v["archetypes"]), "example_traces": v["examples"]}
          for lg, v in sorted(modes.items(), key=lambda kv: -len(kv[1]["traces"]))]
    (run_dir / "failure_modes.json").write_text(json.dumps(
        {"failure_modes": fm, "safety_failures": safety_fail}, ensure_ascii=False, indent=2))

    coverage = {lg: sum(1 for r in out.values() if lg in r["scores"])
                for lg in frozen["evaluators"]}
    print(f"评分覆盖：{coverage}")
    for m in fm:
        print(f"  {m['name']:28s} {m['count']:>3d} 条   原型 {len(m['archetypes'])} 种")
    print(f"safety 不及格：{safety_fail}")
    return 0 if fm and fm[0]["count"] >= 2 else 2


if __name__ == "__main__":
    sys.exit(main())
