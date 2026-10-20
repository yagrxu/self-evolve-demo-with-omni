#!/usr/bin/env python3
"""A/A 校准 + 门禁登记（Omni 流程）。在生成任何候选之前运行。

  python3 training/omni/scripts/gates_register.py --run <run_id> --stage S0

A 组 = Step 6 诊断（diagnosis/cells.json，baseline × 3 遍）
B 组 = 第二次独立的 baseline replay（runs-aa/baseline/invocations.jsonl，由 run_step.sh 6b 产生）
两组是同一个 prompt —— Δ 的波动就是「什么都没改时」的噪声，门槛据此计算。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
from se_eval import composite, load_frozen  # noqa: E402
from se_gates import bootstrap, per_scenario, perf, score_arm  # noqa: E402

TPL = json.loads((REPO / "training" / "omni" / "gates.json").read_text())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--stage", choices=["S0", "S1", "S2"], required=True)
    a = ap.parse_args()
    run_dir = REPO / ".omni" / "self-evolution" / a.run
    out = run_dir / f"gates.registered.{a.stage}.json"
    if out.exists():
        sys.exit(f"{out} 已存在：每个阶段只登记一次")
    cands = [p for p in (run_dir / "scores").glob("*/*.json") if p.stem != "baseline"] if (run_dir / "scores").exists() else []
    if cands:
        sys.exit(f"已有候选分数 {[str(p.relative_to(run_dir)) for p in cands]} —— 看了结果再登记，拒绝")

    frozen = load_frozen(run_dir)
    examples = {e["scenario_id"]: e for e in json.loads((run_dir / "datasets" / "control.json").read_text())["examples"]}
    A_cells = json.loads((run_dir / "diagnosis" / "cells.json").read_text())
    B = score_arm(run_dir, "aa", "baseline", examples, frozen)   # 读 runs-aa/baseline，缓存到 scores/aa/
    A_scn, B_scn = per_scenario(A_cells, frozen), per_scenario(B["cells"], frozen)
    shared = sorted(set(A_scn) & set(B_scn))
    pairs = [(composite(A_scn[s], frozen), composite(B_scn[s], frozen)) for s in shared]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    mean, lo, hi = bootstrap([y - x for x, y in pairs])   # 与 se_gates 同一实现、同一种子
    pa, pb = perf(A_cells), perf(B["cells"])
    half = (hi - lo) / 2
    noise = max(abs(mean), half)
    flip = 1 / (len(pairs) * 3)
    cal = {"n_scenarios": len(pairs), "mean_delta": round(mean, 4), "ci": [round(lo, 4), round(hi, 4)],
           "aa_noise": round(noise, 4), "flip_resolution": round(flip, 4),
           "aa_latency_pct": round((pb["latency_median_ms"] - pa["latency_median_ms"]) / pa["latency_median_ms"] * 100, 2)
           if pa["latency_median_ms"] else None,
           "aa_token_pct": round((pb["token_mean"] - pa["token_mean"]) / pa["token_mean"] * 100, 2)
           if pa["token_mean"] else None}

    biz = TPL["business"]; sg = a.stage; margin = biz["non_inferiority_margin"].get(sg, 0.0)
    g = {"stage": sg, "question": TPL["stages"][sg]["question"], "set": TPL["stages"][sg]["set"],
         "min_scenarios": TPL["stages"][sg]["min_scenarios"],
         "mean_delta_min": max(biz["min_meaningful_improvement"], 2 * noise if sg == "S2" else 0.0),
         "ci_lower_min": 0.0 if sg == "S2" else -margin,
         "per_dim_min": -0.02 if sg == "S2" else -max(margin, flip),
         "token_pct_max": biz["token_budget_pct"], "latency_pct_max": biz["latency_budget_pct"]}
    warn = []
    if g["ci_lower_min"] < 0 and half > -g["ci_lower_min"]:
        warn.append(f"A/A 的 CI 半宽 {half:.3f} > 边界 {-g['ci_lower_min']}：同一个 prompt 也可能过不了 CI 门，"
                    f"需约 {(half / -g['ci_lower_min']) ** 2:.1f}× 有效样本，或由业务在此刻放宽边界")
    for k, lim in (("aa_token_pct", g["token_pct_max"]), ("aa_latency_pct", g["latency_pct_max"])):
        if cal[k] is not None and abs(cal[k]) > lim / 2:
            warn.append(f"A/A 的 {k} = {cal[k]}%，已占预算 {lim}% 的一半以上 —— 成本门对噪声很敏感")
    rec = {"registered_at": datetime.now(timezone.utc).isoformat(), "run": a.run, "gates": g,
           "calibration": cal, "warnings": warn}
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    print(json.dumps({"calibration": cal, "gates": g, "warnings": warn}, ensure_ascii=False, indent=2))
    print(f"✓ 已登记 {out.relative_to(REPO)} —— 从现在起不得修改")
    return 0


if __name__ == "__main__":
    sys.exit(main())
