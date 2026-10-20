#!/usr/bin/env python3
"""Step 8/9 —— 门禁：A/A 校准 → 登记 → 配对比较。全部是确定性算术，不让 LLM 心算。

  # 1) A/A 校准：baseline 跑两个批次，互相比较。测出「什么都没改时 Δ 会抖多少」
  python3 training/cloudwatch/scripts/09_gates.py calibrate --set control --a-batches 1 --b-batches 2

  # 2) 登记：在任何候选结果出现之前，把门禁算成具体数字并冻结（写 gates.registered.json）
  python3 training/cloudwatch/scripts/09_gates.py register --stage S0 --set control
  python3 training/cloudwatch/scripts/09_gates.py register --stage S1 --set confirm   # 在跑 confirm 之前

  # 3) 比较：候选 vs baseline，按 scenario 配对（多批次先取均值）
  python3 training/cloudwatch/scripts/09_gates.py compare --stage S0 --cand c1 --set control --batches 1 2 3

只依赖标准库；输入全部来自 04/05 的产物（traffic / results / tokens）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TR = REPO / "training" / "cloudwatch"
RUN_ID = os.environ.get("RUN_ID", "cwt01")
RUN = TR / "runs" / RUN_ID
GATES = json.loads((REPO / os.environ.get("GATES_FILE", "training/cloudwatch/gates.json")).read_text())
REG = lambda stage: RUN / f"gates.registered.{stage}.json"  # noqa: E731


def jl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def load_arm(arm: str, set_name: str, batches: list[int]) -> dict:
    """→ {scenario: {evaluator: mean_canonical}}, perf。缺失一律记未评分，不当 0。"""
    scales = GATES["scales"]
    per: dict[str, dict[str, list[float]]] = {}
    lat, tok, cells = [], [], 0
    for b in batches:
        tr = {r["session_id"]: r for r in jl(RUN / "traffic" / f"{arm}-{set_name}-b{b}.jsonl")}
        res = jl(RUN / "results" / f"{arm}-{set_name}-b{b}.jsonl")
        if not tr or not res:
            sys.exit(f"缺 {arm} {set_name} 批次 {b} 的 traffic 或 results（先跑 04/05）")
        for r in res:
            t = tr.get(r["session_id"])
            ev = r["evaluator"]
            if t is None or r["score"] is None or ev not in scales:
                continue
            s = scales[ev]
            x = max(0.0, min(1.0, (float(r["score"]) - s["min"]) / (s["max"] - s["min"])))
            per.setdefault(t["scenario_id"], {}).setdefault(ev, []).append(x)
        lat += [r["latency_ms"] for r in tr.values() if r["status"] == "ok"]
        tok += [x["input_tokens"] + x["output_tokens"]
                for x in jl(RUN / "results" / f"{arm}-{set_name}-b{b}.tokens.jsonl")]
        cells += sum(1 for r in tr.values() if r["status"] == "ok")
    return {"by_scn": {s: {e: st.fmean(v) for e, v in d.items()} for s, d in per.items()},
            "latency_median_ms": st.median(lat) if lat else None,
            "token_mean": st.fmean(tok) if tok else None, "cells": cells, "batches": len(batches)}


def composite(d: dict[str, float]) -> float | None:
    q = GATES["quality_evaluators"]
    return st.fmean(d[e] for e in q) if all(e in d for e in q) else None


def bootstrap(diffs: list[float]) -> tuple[float, float, float]:
    b = GATES["bootstrap"]
    rng = random.Random(b["seed"]); n = len(diffs)
    boots = sorted(st.fmean(diffs[rng.randrange(n)] for _ in range(n)) for _ in range(b["iterations"]))
    a = (1 - b["confidence"]) / 2
    return st.fmean(diffs), boots[int(a * len(boots))], boots[min(len(boots) - 1, int((1 - a) * len(boots)))]


def paired(base: dict, cand: dict) -> dict:
    shared = sorted(set(base["by_scn"]) & set(cand["by_scn"]))
    pairs = [(composite(base["by_scn"][s]), composite(cand["by_scn"][s])) for s in shared]
    diffs = [c - b for b, c in pairs if b is not None and c is not None]
    if len(diffs) < 2:
        sys.exit(f"配对样本 {len(diffs)} < 2 —— 不是'没提升'，是没测成（NO_DECISION）")
    mean, lo, hi = bootstrap(diffs)
    per_dim = {}
    for e in GATES["quality_evaluators"] + GATES["guard_evaluators"]:
        ss = [s for s in shared if e in base["by_scn"][s] and e in cand["by_scn"][s]]
        per_dim[e] = (round(st.fmean(cand["by_scn"][s][e] for s in ss)
                            - st.fmean(base["by_scn"][s][e] for s in ss), 4) if ss else None)
    new_fail = []
    for e in GATES["safety_evaluators"]:
        sc = GATES["scales"][e]; thr = (sc["pass"] - sc["min"]) / (sc["max"] - sc["min"])
        for s in shared:
            b, c = base["by_scn"][s].get(e), cand["by_scn"][s].get(e)
            if c is None:
                new_fail.append(f"{s}:{e}(未评分)")
            elif c < thr and (b is None or b >= thr):
                new_fail.append(f"{s}:{e}")
    pct = lambda a, b: None if not a or b is None else round((b - a) / a * 100, 2)  # noqa: E731
    return {"n_scenarios": len(diffs), "mean_delta": round(mean, 4), "ci": [round(lo, 4), round(hi, 4)],
            "better": sum(d > 1e-3 for d in diffs), "worse": sum(d < -1e-3 for d in diffs),
            "per_dim_delta": per_dim, "safety_new_failures": new_fail,
            "latency_pct": pct(base["latency_median_ms"], cand["latency_median_ms"]),
            "token_pct": pct(base["token_mean"], cand["token_mean"]),
            "flip_resolution": round(1 / (len(diffs) * min(base["batches"], cand["batches"])), 4)}


def cmd_calibrate(a: argparse.Namespace) -> None:
    A = load_arm("baseline", a.set, a.a_batches)
    B = load_arm("baseline", a.set, a.b_batches)
    r = paired(A, B)
    out = {"what": "A/A：同一个 baseline 的两组独立批次互相比较。理想值 Δ=0；偏离多少就是噪声有多大。",
           "set": a.set, "a_batches": a.a_batches, "b_batches": a.b_batches, **r,
           "aa_noise": round(max(abs(r["mean_delta"]), (r["ci"][1] - r["ci"][0]) / 2), 4),
           "at": datetime.now(timezone.utc).isoformat()}
    (RUN / "calibration.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({k: out[k] for k in ("n_scenarios", "mean_delta", "ci", "aa_noise", "flip_resolution")},
                     ensure_ascii=False))
    print("→ aa_noise 是「什么都没改」时的 Δ 波动；S2 的最小提升必须明显大于它")


def cmd_register(a: argparse.Namespace) -> None:
    if REG(a.stage).exists() and not a.force:
        sys.exit(f"{REG(a.stage)} 已存在。每个阶段只登记一次；要重新登记必须换 RUN_ID")
    cand_results = [p for p in (RUN / "results").glob(f"*-{a.set}-b*.jsonl")
                    if not p.name.startswith("baseline-") and not p.name.endswith(".tokens.jsonl")]
    if cand_results:
        sys.exit(f"已经有候选结果 {[p.name for p in cand_results]} —— 现在登记就是看了答案再定标准，拒绝")
    cal = json.loads((RUN / "calibration.json").read_text()) if (RUN / "calibration.json").exists() else None
    if cal is None:
        sys.exit("先跑 calibrate（A/A）—— 不知道噪声多大，门槛就是拍脑袋")
    biz = GATES["business"]; sg = a.stage
    margin = biz["non_inferiority_margin"].get(sg, 0.0)
    g = {"stage": sg, "question": GATES["stages"][sg]["question"],
         "min_scenarios": GATES["stages"][sg]["min_scenarios"],
         "mean_delta_min": (max(biz["min_meaningful_improvement"], 2 * cal["aa_noise"]) if sg == "S2"
                            else max(biz["min_meaningful_improvement"], 0.0)),
         "ci_lower_min": 0.0 if sg == "S2" else -margin,
         "per_dim_min": -0.02 if sg == "S2" else -max(margin, cal["flip_resolution"]),
         "token_pct_max": biz["token_budget_pct"], "latency_pct_max": biz["latency_budget_pct"],
         "safety_new_failures_max": 0}
    # 可达性检查：A/A（什么都没改）时 CI 的半宽如果已经超过非劣效边界，
    # 那么即使候选与 baseline 完全相同也过不了 CI 门 —— 门禁在当前样本量下不可达。
    half = (cal["ci"][1] - cal["ci"][0]) / 2
    feas = {"aa_ci_halfwidth": round(half, 4), "ci_lower_min": g["ci_lower_min"],
            "reachable": (half <= abs(g["ci_lower_min"])) if g["ci_lower_min"] < 0 else None,
            "advice": ""}
    if g["ci_lower_min"] < 0 and half > abs(g["ci_lower_min"]):
        k = (half / abs(g["ci_lower_min"])) ** 2
        feas["advice"] = (f"A/A 的 CI 半宽 {half:.3f} > 边界 {abs(g['ci_lower_min'])}：同一个 prompt 也过不了。"
                          f"需要约 {k:.1f}× 的有效样本（更多批次或更多 scenario），或由业务放宽边界——在登记前决定")
        print("⚠ " + feas["advice"])
    g["feasibility"] = feas
    rec = {"registered_at": datetime.now(timezone.utc).isoformat(), "run_id": RUN_ID, "gates": g,
           "calibration": cal, "template_sha256": hashlib.sha256(json.dumps(GATES, sort_keys=True).encode()).hexdigest(),
           "evaluators": {k: GATES[k] for k in ("quality_evaluators", "guard_evaluators", "safety_evaluators", "report_only")}}
    rec["set"] = a.set
    REG(a.stage).write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    print(json.dumps(g, ensure_ascii=False, indent=2))
    print(f"✓ 已登记 {REG(a.stage).relative_to(REPO)}（{rec['registered_at']}）—— 从现在起不得修改")


def cmd_compare(a: argparse.Namespace) -> None:
    if not REG(a.stage).exists():
        sys.exit(f"阶段 {a.stage} 还没登记门禁（register）。没有事先登记的比较不能作为结论")
    reg = json.loads(REG(a.stage).read_text()); g = reg["gates"]
    sets = a.set.split(",")
    if sets[0] != reg.get("set"):
        sys.exit(f"阶段 {a.stage} 登记的集合是 {reg.get('set')}，不是 {sets[0]}")
    extra = sets[1:]
    if extra:   # 扩充确认集必须事先记录（gates.expansion.<stage>.json），且扩充的集合在记录时还没有结果
        ep = RUN / f"gates.expansion.{a.stage}.json"
        if not ep.exists() or sorted(json.loads(ep.read_text())["added_sets"]) != sorted(extra):
            sys.exit(f"扩充集合 {extra} 没有事先记录在 {ep.name}")
    def merged(arm):
        parts = [load_arm(arm, s_, a.batches) for s_ in sets]
        out = {"by_scn": {}, "cells": 0, "batches": len(a.batches)}
        lat, tok = [], []
        for p_ in parts:
            out["by_scn"].update(p_["by_scn"]); out["cells"] += p_["cells"]
        # 延迟 / token：按格数加权合并（各集合格数相同，取均值即可）
        ms = [p_["latency_median_ms"] for p_ in parts if p_["latency_median_ms"]]
        tm = [p_["token_mean"] for p_ in parts if p_["token_mean"]]
        out["latency_median_ms"] = st.fmean(ms) if ms else None
        out["token_mean"] = st.fmean(tm) if tm else None
        return out
    r = paired(merged(a.base), merged(a.cand))
    # 门禁校准（人工决定，记录在 gates.calibration.<stage>.json）：只改门的判定方式，不改数据
    calp = RUN / f"gates.calibration.{a.stage}.json"
    cal = json.loads(calp.read_text()) if calp.exists() else None
    ov = (cal or {}).get("overrides", {})
    dims = {k: v for k, v in r["per_dim_delta"].items() if k not in ov.get("exclude_per_dim", [])}
    sf = [x for x in r["safety_new_failures"] if x.split(":")[-1].split("(")[0] not in ov.get("safety_report_only", [])]
    g = {**g, **{k: v for k, v in ov.items() if k in ("token_pct_max", "latency_pct_max", "min_scenarios")}}
    worst_name, worst = min(((k, v) for k, v in dims.items() if v is not None),
                            key=lambda kv: kv[1], default=(None, None))
    checks = [
        ("样本量", r["n_scenarios"], f">= {g['min_scenarios']}", r["n_scenarios"] >= g["min_scenarios"]),
        ("均值提升", r["mean_delta"], f">= {g['mean_delta_min']}", r["mean_delta"] >= g["mean_delta_min"]),
        ("95% CI 下界", r["ci"][0], f">= {g['ci_lower_min']}", r["ci"][0] >= g["ci_lower_min"]),
        ("最差单维", worst, f">= {round(g['per_dim_min'], 4)}（{worst_name}）",
         worst is not None and worst >= g["per_dim_min"] and None not in dims.values()),
        ("延迟回退 %", r["latency_pct"], f"<= {g['latency_pct_max']}",
         r["latency_pct"] is not None and r["latency_pct"] <= g["latency_pct_max"]),
        ("token 回退 %", r["token_pct"], f"<= {g['token_pct_max']}",
         r["token_pct"] is not None and r["token_pct"] <= g["token_pct_max"]),
        ("safety 新增失败", len(sf), "== 0", not sf),
    ]
    passed = all(c[3] for c in checks)
    verdict = ("PASS_UNDER_CALIBRATED_GATES" if cal else "PASS") if passed else "FAIL"
    out = {"stage": g["stage"], "cand": a.cand, "base": a.base, "set": a.set, "batches": a.batches, "verdict": verdict,
           "calibration": cal,
           "registered_at": reg["registered_at"], "compared_at": datetime.now(timezone.utc).isoformat(),
           "checks": [{"gate": c[0], "value": c[1], "threshold": c[2], "passed": c[3]} for c in checks], **r}
    p = RUN / f"gates-{a.stage}-{a.cand}-{a.set.replace(',', '+')}{'' if a.base == 'baseline' else '-vs-' + a.base}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"阶段 {g['stage']}（{g['question']}） · 候选 {a.cand} vs {a.base} · {a.set} 批次 {a.batches}")
    print(f"配对 {r['n_scenarios']} 个 scenario：变好 {r['better']} / 变差 {r['worse']}")
    for c in checks:
        print(f"  {'✓' if c[3] else '✗'} {c[0]:12s} {str(c[1]):>10s}   门槛 {c[2]}")
    print("  逐维 Δ：" + "  ".join(f"{k.split('.')[-1]} {v:+.3f}" for k, v in r["per_dim_delta"].items() if v is not None))
    if sf:
        print("  safety：" + "；".join(sf[:6]))
    if cal:
        print(f"  ⚖ 门禁校准（{cal['decided_at']}，{cal['by']}，calibrated_after_results={cal['calibrated_after_results']}）：{cal['reason']}")
        if r["safety_new_failures"] != sf:
            print("  safety（仅报告，不拦截）：" + "；".join(x for x in r["safety_new_failures"] if x not in sf))
    print(f"→ {verdict}   （{p.relative_to(REPO)}）")


def cmd_expand_set(a: argparse.Namespace) -> None:
    """记录扩充确认集的决定。必须在被扩充集合出现任何结果之前。"""
    p = RUN / f"gates.expansion.{a.stage}.json"
    if p.exists():
        sys.exit(f"{p} 已存在")
    seen = [x.name for s_ in a.add for x in (RUN / "results").glob(f"*-{s_}-b*.jsonl")]
    if seen:
        sys.exit(f"{a.add} 已经有结果 {seen} —— 看了结果再扩充，拒绝")
    rec = {"stage": a.stage, "decided_at": datetime.now(timezone.utc).isoformat(), "by": a.by,
           "reason": a.reason, "added_sets": a.add, "results_seen_for_added_sets": []}
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    print(json.dumps(rec, ensure_ascii=False, indent=2))


def cmd_calibrate_gates(a: argparse.Namespace) -> None:
    """记录一次人工门禁校准。不改登记文件本身；compare 会叠加它并把结局标为 *_UNDER_CALIBRATED_GATES。"""
    p = RUN / f"gates.calibration.{a.stage}.json"
    prev = json.loads(p.read_text()) if p.exists() else None
    if prev and not a.amend:
        sys.exit(f"{p} 已存在：追加校准必须显式 --amend（会保留完整历史）")
    seen = sorted(x.name for x in RUN.glob(f"gates-{a.stage}-*.json"))
    ov = dict((prev or {}).get("overrides", {}))
    if a.exclude_dim: ov["exclude_per_dim"] = sorted(set(ov.get("exclude_per_dim", [])) | set(a.exclude_dim))
    if a.safety_report_only: ov["safety_report_only"] = sorted(set(ov.get("safety_report_only", [])) | set(a.safety_report_only))
    if a.token_pct_max is not None: ov["token_pct_max"] = a.token_pct_max
    rec = {"stage": a.stage, "decided_at": datetime.now(timezone.utc).isoformat(), "by": a.by,
           "reason": a.reason if not prev else prev["reason"] + " ‖ 追加：" + a.reason,
           "calibrated_after_results": bool(seen) or bool((prev or {}).get("calibrated_after_results")),
           "results_seen_before_decision": seen, "overrides": ov,
           "history": (prev or {}).get("history", []) + ([{k: prev[k] for k in prev if k != "history"}] if prev else [])}
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    print(json.dumps(rec, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate"); c.add_argument("--set", default="control")
    c.add_argument("--a-batches", type=int, nargs="+", required=True)
    c.add_argument("--b-batches", type=int, nargs="+", required=True)
    r = sub.add_parser("register"); r.add_argument("--stage", choices=["S0", "S1", "S2"], required=True)
    r.add_argument("--set", default="control", help="该阶段用哪份数据集（S0=control，S1=confirm）")
    r.add_argument("--force", action="store_true")
    m = sub.add_parser("compare"); m.add_argument("--cand", required=True)
    m.add_argument("--stage", choices=["S0", "S1", "S2"], required=True)
    m.add_argument("--base", default="baseline", help="对照 arm（默认 baseline；数据层不同步时用重新打包的 baseline，如 b0）")
    m.add_argument("--set", default="control"); m.add_argument("--batches", type=int, nargs="+", required=True)
    k = sub.add_parser("calibrate-gates"); k.add_argument("--stage", choices=["S0", "S1", "S2"], required=True)
    k.add_argument("--by", required=True); k.add_argument("--reason", required=True)
    k.add_argument("--exclude-dim", nargs="*", default=[]); k.add_argument("--safety-report-only", nargs="*", default=[])
    k.add_argument("--token-pct-max", type=float)
    k.add_argument("--amend", action="store_true", help="在已有校准上追加一次（保留历史）")
    x = sub.add_parser("expand-set"); x.add_argument("--stage", choices=["S0", "S1", "S2"], required=True)
    x.add_argument("--add", nargs="+", required=True); x.add_argument("--by", required=True); x.add_argument("--reason", required=True)
    a = ap.parse_args()
    {"calibrate": cmd_calibrate, "register": cmd_register, "compare": cmd_compare,
     "calibrate-gates": cmd_calibrate_gates, "expand-set": cmd_expand_set}[a.cmd](a)


if __name__ == "__main__":
    main()
