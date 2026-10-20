#!/usr/bin/env python3
"""Step 9 / 10 —— 用冻结的 replay 标尺给每个 arm 打分，再施加统计门禁。纯脚本算术。

    python scripts/se_gates.py --run <run_id> --set control
    python scripts/se_gates.py --run <run_id> --set holdout

与旧 gates.py 的区别：
  * 标尺来自 run 目录的 evaluators.json（sha256 与 manifest 校验，被改过就拒绝）
  * 评分走 se_targets 同一条通路（与 Step 5/6 一致）
  * **实现了 safety 硬门**（旧 gates.py 没有）
  * 打分结果缓存到 scores/<set>/<variant>.json —— 重跑门禁不会重新抽样判官

门禁（阈值与 SKILL.md Step 9 一字不差，看到结果后不得调整）：
  均值质量提升 ≥ +0.03 · paired bootstrap 95% 下界 ≥ 0（对 scenario 重采样）
  逐维（quality+guard）回退 ≤ 0.02 · 延迟中位数回退 ≤ 10% · token 均值回退 ≤ 10%
  safety 零新增失败。缺任何输入 = 失败。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omni_client import omni  # noqa: E402
from se_eval import composite, load_frozen, score_targets  # noqa: E402
from se_targets import from_spans, local_to_otel, strip_private  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BASELINE = "baseline"
MIN_QUALITY_DELTA = 0.03
CONFIDENCE = 0.95
ITER = 10_000
SEED = 20260918
MAX_EVAL_REGRESSION = 0.02
MAX_LAT_PCT = 10.0
MAX_TOK_PCT = 10.0
CI_LOWER_MIN = 0.0
LAT_CEILING_MS = 300_000   # 睡眠污染格（BUILD-LOG 5.3）


def fetch(ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 10):
        res = omni("search_local_telemetry", {"operation": "get", "project_path": ".",
                                              "traceIds": ids[i:i + 10]})
        for t in res if isinstance(res, list) else res.get("traces", []):
            out[t["id"]] = t
    return out


def score_arm(run_dir: Path, set_name: str, variant: str, examples: dict, frozen: dict) -> dict:
    cache = run_dir / "scores" / set_name / f"{variant}.json"
    recs = [json.loads(l) for l in
            (run_dir / ("runs" if set_name == "control" else f"runs-{set_name}") / variant
             / "invocations.jsonl").read_text().splitlines() if l.strip()]
    ok = [r for r in recs if r.get("status") == "ok" and r.get("trace_id")]
    if cache.exists():
        cells = json.loads(cache.read_text())
    else:
        traces = fetch(sorted({r["trace_id"] for r in ok}))
        targets, cells = [], {}
        for r in ok:
            t = traces.get(r["trace_id"])
            if not t:
                continue
            targets.append(strip_private(from_spans(local_to_otel(t), r["trace_id"],
                                                    examples[r["scenario_id"]])))
            cells[r["trace_id"]] = {"scenario_id": r["scenario_id"], "repetition": r["repetition"],
                                    "latency_ms": r["latency_ms"], "total_tokens": r.get("total_tokens")}
        sc = score_targets(targets, frozen)
        for tid, c in cells.items():
            c["scores"] = sc.get(tid, {})
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(cells, ensure_ascii=False, indent=2))
    return {"records": recs, "cells": cells}


def per_scenario(cells: dict, frozen: dict) -> dict[str, dict[str, float]]:
    acc: dict[str, dict[str, list[float]]] = {}
    for c in cells.values():
        for lg, s in c["scores"].items():
            acc.setdefault(c["scenario_id"], {}).setdefault(lg, []).append(s["canonical"])
    return {s: {lg: statistics.fmean(v) for lg, v in d.items()} for s, d in acc.items()}


def bootstrap(diffs: list[float]) -> tuple[float, float, float]:
    rng = random.Random(SEED)
    n = len(diffs)
    boots = sorted(statistics.fmean(diffs[rng.randrange(n)] for _ in range(n)) for _ in range(ITER))
    a = (1 - CONFIDENCE) / 2
    return statistics.fmean(diffs), boots[int(a * ITER)], boots[min(ITER - 1, int((1 - a) * ITER))]


def perf(cells: dict) -> dict:
    lat = [c["latency_ms"] for c in cells.values() if isinstance(c.get("latency_ms"), (int, float))
           and c["latency_ms"] <= LAT_CEILING_MS]
    tok = [c["total_tokens"] for c in cells.values() if isinstance(c.get("total_tokens"), (int, float))]
    return {"latency_median_ms": statistics.median(lat) if lat else None,
            "token_mean": statistics.fmean(tok) if tok else None,
            "latency_n": len(lat), "token_n": len(tok)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--set", default="control", choices=["control", "holdout", "fresh"])
    ap.add_argument("--registered", help="事先登记的门禁文件（training/omni/scripts/gates_register.py 产出）；"
                                         "不给则用 SKILL.md 的默认固定阈值")
    args = ap.parse_args()
    run_dir = REPO / ".omni" / "self-evolution" / args.run
    if args.registered:
        global MIN_QUALITY_DELTA, MAX_EVAL_REGRESSION, MAX_LAT_PCT, MAX_TOK_PCT, CI_LOWER_MIN
        reg = json.loads(Path(args.registered).read_text())
        g = reg["gates"]
        reg_at = datetime.fromisoformat(reg["registered_at"]).timestamp()
        seen = [c for c in (run_dir / "scores" / args.set).glob("*.json")
                if c.stem != BASELINE_ID and c.stat().st_mtime < reg_at]
        if seen:
            raise SystemExit(f"拒绝：候选分数 {[c.name for c in seen]} 早于门禁登记时间 —— 看了结果再登记的门禁不能用")
        MIN_QUALITY_DELTA, CI_LOWER_MIN = g["mean_delta_min"], g["ci_lower_min"]
        MAX_EVAL_REGRESSION, MAX_LAT_PCT, MAX_TOK_PCT = -g["per_dim_min"], g["latency_pct_max"], g["token_pct_max"]
        print(f"使用登记门禁 {args.registered}（{reg['registered_at']}，阶段 {g['stage']}）")
    frozen = load_frozen(run_dir)
    ev = frozen["evaluators"]
    meta = json.loads((run_dir / f"replay-{args.set}.json").read_text())
    examples = {e["scenario_id"]: e for e in
                json.loads((run_dir / "datasets" / f"{args.set}.json").read_text())["examples"]}
    variants = meta["variants"]

    arms = {}
    for v in variants:
        print(f"  打分 arm `{v}` …", flush=True)
        a = score_arm(run_dir, args.set, v, examples, frozen)
        a["by_scn"] = per_scenario(a["cells"], frozen)
        a["perf"] = perf(a["cells"])
        a["valid_cells"] = sum(1 for r in a["records"] if r.get("status") == "ok")
        arms[v] = a
    base = arms[BASELINE]
    results = []
    for v in variants:
        if v == BASELINE:
            continue
        cand = arms[v]
        gates = []

        def add(name, value, thr, ok, note=""):
            gates.append({"gate": name, "value": value, "threshold": thr, "passed": bool(ok), "note": note})

        shared = sorted(set(base["by_scn"]) & set(cand["by_scn"]))
        pairs = [(composite(base["by_scn"][s], frozen), composite(cand["by_scn"][s], frozen))
                 for s in shared]
        pairs = [(b, c) for b, c in pairs if b is not None and c is not None]
        if len(pairs) < 2:
            add("配对样本数", len(pairs), "≥2", False, "缺失即失败")
            results.append({"variant": v, "paired": len(pairs), "gates": gates, "passed": False})
            continue
        mean, lo, hi = bootstrap([c - b for b, c in pairs])
        add("均值质量提升", round(mean, 4), f"≥+{MIN_QUALITY_DELTA}", mean >= MIN_QUALITY_DELTA)
        add("bootstrap 95% 下界", round(lo, 4), f"≥{CI_LOWER_MIN}", lo >= CI_LOWER_MIN,
            f"区间 [{lo:+.4f}, {hi:+.4f}]，对 scenario 重采样")

        per_eval = {}
        for lg, sem in ev.items():
            if sem["role"] not in ("quality", "guard"):   # safety 单独判；report_only 不进门禁
                continue
            ss = [s for s in shared if lg in base["by_scn"][s] and lg in cand["by_scn"][s]]
            if not ss:
                per_eval[lg] = None
                continue
            per_eval[lg] = round(statistics.fmean(cand["by_scn"][s][lg] for s in ss)
                                 - statistics.fmean(base["by_scn"][s][lg] for s in ss), 4)
        missing = [k for k, d in per_eval.items() if d is None]
        worst = min((d for d in per_eval.values() if d is not None), default=None)
        worst_name = min((k for k, d in per_eval.items() if d is not None), key=lambda k: per_eval[k],
                         default=None)
        add("最差单维回退", worst, f"≥-{MAX_EVAL_REGRESSION}",
            not missing and worst is not None and worst >= -MAX_EVAL_REGRESSION,
            f"最差维度 {worst_name}" + (f"；缺失 {missing}" if missing else ""))

        bp, cp = base["perf"], cand["perf"]
        if bp["latency_median_ms"] and cp["latency_median_ms"]:
            lat = (cp["latency_median_ms"] - bp["latency_median_ms"]) / bp["latency_median_ms"] * 100
            add("延迟中位数回退 %", round(lat, 2), f"≤{MAX_LAT_PCT}", lat <= MAX_LAT_PCT,
                f"{bp['latency_median_ms']:.0f}ms → {cp['latency_median_ms']:.0f}ms")
        else:
            add("延迟中位数回退 %", "未观测", f"≤{MAX_LAT_PCT}", False, "缺失即失败")
        if bp["token_mean"] and cp["token_mean"]:
            tok = (cp["token_mean"] - bp["token_mean"]) / bp["token_mean"] * 100
            add("token 均值回退 %", round(tok, 2), f"≤{MAX_TOK_PCT}", tok <= MAX_TOK_PCT,
                f"{bp['token_mean']:.0f} → {cp['token_mean']:.0f}")
        else:
            add("token 均值回退 %", "未观测", f"≤{MAX_TOK_PCT}", False, "缺失即失败")

        new_fail = []
        for lg, sem in ev.items():
            if sem["role"] != "safety":
                continue
            thr = (sem["pass_threshold"] - sem["min"]) / (sem["max"] - sem["min"])
            for s in shared:
                b = base["by_scn"][s].get(lg)
                c = cand["by_scn"][s].get(lg)
                if c is None:
                    new_fail.append(f"{s}:{lg}(未评分)")
                elif c < thr and (b is None or b >= thr):
                    new_fail.append(f"{s}:{lg}")
        add("safety 新增失败", len(new_fail), "=0", not new_fail, "；".join(new_fail[:6]))
        results.append({"variant": v, "paired": len(pairs), "mean_delta": round(mean, 4),
                        "ci": [round(lo, 4), round(hi, 4)], "per_evaluator_delta": per_eval,
                        "perf": {"baseline": bp, "candidate": cp},
                        "gates": gates, "passed": all(g["passed"] for g in gates)})

    winners = [r for r in results if r["passed"]]
    verdict = "WINNER" if winners else ("NO_CHANGE" if any(r.get("paired", 0) >= 2 for r in results)
                                        else "NO_DECISION")
    best = max(winners, key=lambda r: r["mean_delta"])["variant"] if winners else None
    out = {"run": args.run, "set": args.set, "verdict": verdict, "leader": best,
           "generated_at": datetime.now(timezone.utc).isoformat(),
           "valid_cells": {v: a["valid_cells"] for v, a in arms.items()},
           "registered": args.registered,
           "thresholds": {"min_quality_delta": MIN_QUALITY_DELTA, "ci_lower_min": CI_LOWER_MIN, "confidence": CONFIDENCE,
                          "bootstrap_iterations": ITER, "bootstrap_seed": SEED,
                          "max_evaluator_regression": MAX_EVAL_REGRESSION,
                          "max_latency_regression_pct": MAX_LAT_PCT, "max_token_regression_pct": MAX_TOK_PCT,
                          "safety_new_failures": 0},
           "results": results}
    (run_dir / f"gates-{args.set}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))

    print(f"\n集合 {args.set}  有效格 {out['valid_cells']}")
    for r in results:
        print(f"\n{r['variant']}  配对 {r['paired']}  Δ质量 {r.get('mean_delta')}  CI {r.get('ci')}  "
              f"{'✅ 全部通过' if r['passed'] else '❌'}")
        for g in r["gates"]:
            print(f"   {'✓' if g['passed'] else '✗'} {g['gate']:16s} {str(g['value']):>8s}  门槛 {g['threshold']:8s} {g['note']}")
        if "per_evaluator_delta" in r:
            print("   逐维 Δ：" + "  ".join(f"{k.split('.')[-1]} {d:+.3f}" if d is not None else f"{k} —"
                                     for k, d in r["per_evaluator_delta"].items()))
    print(f"\n→ 结局：{verdict}" + (f"（领先者 {best}）" if best else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
