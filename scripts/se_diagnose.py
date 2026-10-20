#!/usr/bin/env python3
"""Step 6 —— 基线在 control 上重复跑之后，逐格打分 + 确定性诊断 + 普遍性统计。

输入：<run_dir>/diagnosis/runs/baseline/invocations.jsonl（replay.py 产出后移入）
产出：<run_dir>/diagnosis/
  traces.jsonl   每格的本地 trace（原样）
  cells.json     每格：分数、判词、确定性诊断
  summary.json   普遍性：每个 evaluator / 确定性检查的失败格数、每个 scenario 的 k/3、按原型

只读 control。对话里只报条数与比例，不贴分数表。

    python scripts/se_diagnose.py --run <run_id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omni_client import omni  # noqa: E402
from se_eval import load_frozen, score_targets  # noqa: E402
from se_targets import from_spans, local_to_otel, strip_private  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def in_order(actual: list[str], expected: list[str]) -> bool:
    it = iter(actual)
    return all(any(a == e for a in it) for e in expected)


def fetch(trace_ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(trace_ids), 10):
        res = omni("search_local_telemetry", {"operation": "get", "project_path": ".",
                                              "traceIds": trace_ids[i:i + 10]})
        for t in res if isinstance(res, list) else res.get("traces", []):
            out[t["id"]] = t
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = REPO / ".omni" / "self-evolution" / args.run
    diag = run_dir / "diagnosis"
    frozen = load_frozen(run_dir)
    examples = {e["scenario_id"]: e for e in
                json.loads((run_dir / "datasets" / "control.json").read_text())["examples"]}

    recs = [json.loads(l) for l in
            (diag / "runs" / "baseline" / "invocations.jsonl").read_text().splitlines() if l.strip()]
    ok = [r for r in recs if r.get("status") == "ok" and r.get("trace_id")]
    print(f"格子：{len(recs)}，有效 {len(ok)}")
    traces = fetch(sorted({r["trace_id"] for r in ok}))
    with (diag / "traces.jsonl").open("w", encoding="utf-8") as fh:
        for tid, t in traces.items():
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")

    targets, cells = [], {}
    for r in ok:
        t = traces.get(r["trace_id"])
        if not t:
            continue
        ex = examples[r["scenario_id"]]
        tg = from_spans(local_to_otel(t), r["trace_id"], ex)
        targets.append(strip_private(tg))
        tools = [c["name"] for c in tg["toolCalls"]]
        order_id = ex["metadata"]["order_id"]
        get_order_ids = [str(c["parameters"].get("order_id", "")) for c in tg["toolCalls"]
                         if c["name"] == "get_order"]
        out_text = tg["output"]
        cells[r["trace_id"]] = {
            "scenario_id": r["scenario_id"], "repetition": r["repetition"],
            "archetype": ex["metadata"]["archetype"], "latency_ms": r["latency_ms"],
            "total_tokens": r.get("total_tokens"), "tools": tools, "output": out_text,
            "deterministic": {
                "trajectory_in_order": in_order(tools, ex["expected_trajectory"]),
                # 本地 span 不带工具入参时记为 None（未观测），不当作失败
                "order_id_param": (None if not get_order_ids or get_order_ids == [""]
                                   else all(x == order_id for x in get_order_ids)),
                "label_before_confirmation": "create_return_label" in tools,
                "states_amount": bool(re.search(r"\d+(\.\d+)?\s*元|¥\s*\d", out_text)),
            }}
    print(f"评分项：{len(targets)}")
    scores = score_targets(targets, frozen)
    for tid, c in cells.items():
        c["scores"] = scores.get(tid, {})
    (diag / "cells.json").write_text(json.dumps(cells, ensure_ascii=False, indent=2))

    # ── 普遍性 ───────────────────────────────────────────────────────────
    ev = frozen["evaluators"]
    fail = lambda lg, s: s["raw"] < ev[lg]["pass_threshold"]  # noqa: E731
    by_eval = {lg: sum(1 for c in cells.values() if lg in c["scores"] and fail(lg, c["scores"][lg]))
               for lg in ev}
    covered = {lg: sum(1 for c in cells.values() if lg in c["scores"]) for lg in ev}
    det = {k: sum(1 for c in cells.values() if c["deterministic"][k] is False)
           for k in ("trajectory_in_order", "order_id_param")}
    det["label_before_confirmation"] = sum(1 for c in cells.values()
                                           if c["deterministic"]["label_before_confirmation"])
    per_scn: dict[str, dict] = defaultdict(lambda: {"reps": 0, "quality_fail_reps": 0,
                                                    "failed_evaluators": Counter()})
    for c in cells.values():
        p = per_scn[c["scenario_id"]]
        p["archetype"] = c["archetype"]
        p["reps"] += 1
        bad = [lg for lg, s in c["scores"].items() if ev[lg]["role"] in ("quality", "guard") and fail(lg, s)]
        if bad:
            p["quality_fail_reps"] += 1
        p["failed_evaluators"].update(bad)
    systematic = sorted(s for s, p in per_scn.items() if p["reps"] and p["quality_fail_reps"] == p["reps"])
    sporadic = sorted(s for s, p in per_scn.items() if 0 < p["quality_fail_reps"] < p["reps"])
    clean = sorted(s for s, p in per_scn.items() if p["quality_fail_reps"] == 0)
    summary = {"cells": len(cells), "evaluator_fail_cells": by_eval, "evaluator_covered_cells": covered,
               "deterministic_fail_cells": det,
               "scenarios": {"total": len(per_scn), "systematic_all_reps_fail": systematic,
                             "sporadic": sporadic, "clean": clean},
               "per_scenario": {s: {**p, "failed_evaluators": dict(p["failed_evaluators"])}
                                for s, p in sorted(per_scn.items())}}
    (diag / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print(f"\n{'evaluator':34s} {'不及格格数':>8s} / 覆盖")
    for lg in ev:
        print(f"  {lg:32s} {by_eval[lg]:>6d} / {covered[lg]}")
    print(f"确定性诊断失败格数：{det}")
    print(f"scenario：系统性（每遍都失败）{len(systematic)}  偶发 {len(sporadic)}  "
          f"全部通过 {len(clean)}  / {len(per_scn)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
