#!/usr/bin/env python3
"""SKILL.md Step 9 —— 对 replay 结果施加统计门禁，算出每个候选的判决。

这一步**必须是脚本**（SKILL.md Step 9 的硬约束）：delta / bootstrap 区间 / 逐 evaluator
判定都有唯一正确答案，而多 arm 分数表上的 LLM 算术不可靠。agent 不得在自己的输出里
重算这些数字。

六道门（阈值及其论证见 SKILL.md Step 9 的表格）
----------------------------------------------
| 门 | 阈值 |
|---|---|
| 均值质量提升 | ≥ +0.03（归一化后）|
| paired bootstrap 置信区间 | 95%，下界 ≥ 0 |
| 逐 evaluator 回退 | 每个 ≤ 0.02 |
| 延迟回退 | ≤ 10% |
| token 回退 | ≤ 10% |
| safety | 零新增失败（硬门）|

bootstrap 对**样本**重采样，不对调用重采样 —— 对调用重采样估的是"这批样本测得多准"，
而我们要问的是"能否泛化到新输入"。

用法：
    python scripts/gates.py --run <run_id> --set control
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
from omni_client import OmniError, omni  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BASELINE_ID = "baseline"

# ── 门禁阈值。改动等于改变结论的可信度，必须同步改 SKILL.md Step 9 ──
MIN_QUALITY_DELTA = 0.03
BOOTSTRAP_CONFIDENCE = 0.95
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20260918          # 固定种子：区间必须可复现
MAX_EVALUATOR_REGRESSION = 0.02
MAX_LATENCY_REGRESSION_PCT = 10.0
MAX_TOKEN_REGRESSION_PCT = 10.0


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def canonical(score: float, sem: dict) -> float:
    lo, hi = float(sem["min"]), float(sem["max"])
    if hi == lo:
        raise ValueError(f"evaluator 值域非法: min==max=={lo}")
    x = max(0.0, min(1.0, (float(score) - lo) / (hi - lo)))
    return x if sem["direction"] == "HIGHER_IS_BETTER" else 1.0 - x


def score_arm(variant: str, records: list[dict], semantics: dict,
              run_dir: Path) -> dict[str, dict[str, float]]:
    """用冻结的 evaluator 给一个 arm 的 trace 打分，返回 {scenario: {evaluator: 均值}}。

    只评 ``status == "ok"`` 的格子 —— 被版本核对丢弃的格子绝不参与。
    """
    ok = [r for r in records if r.get("status") == "ok" and r.get("trace_id")]
    if not ok:
        return {}
    trace_ids = sorted({r["trace_id"] for r in ok})
    by_trace = {r["trace_id"]: r["scenario_id"] for r in ok}

    per_scenario: dict[str, dict[str, list[float]]] = {}
    for logical, sem in semantics.items():
        if not sem.get("per_example", True):
            continue  # R10：SESSION 级在本地只有 1 个有效观测，不进门禁
        try:
            res = omni("manage_evaluations", {
                "action": "run", "project_path": ".",
                "evaluatorId": sem["evaluator_id"], "traceIds": trace_ids,
                "dataSource": "local", "evaluatorLevel": sem["level"]},
                timeout_s=2400)
        except OmniError as e:
            print(f"    ⚠️ {logical} 评估失败：{e}")
            continue
        rows = res.get("results") or res.get("evaluations") or []
        for row in rows if isinstance(rows, list) else []:
            key = row.get("itemKey") or row.get("traceId") or ""
            scenario = by_trace.get(key)
            raw = row.get("score", row.get("value"))
            # -1 是 Omni 的 unscored 约定；缺失一律记为未评分，绝不当 0
            if scenario is None or raw is None or raw == -1:
                continue
            try:
                v = canonical(float(raw), sem)
            except (TypeError, ValueError):
                continue
            per_scenario.setdefault(scenario, {}).setdefault(logical, []).append(v)

    return {s: {k: statistics.fmean(v) for k, v in d.items() if v}
            for s, d in per_scenario.items()}


def composite(scores: dict[str, float], semantics: dict) -> float | None:
    """等权综合质量分。缺失的维度不参与（不补 0）。"""
    vals = [v for k, v in scores.items() if semantics.get(k, {}).get("per_example", True)]
    return statistics.fmean(vals) if vals else None


def paired_bootstrap(pairs: list[tuple[float, float]], iterations: int,
                     confidence: float, seed: int) -> tuple[float, float, float]:
    """对**样本**重采样，返回 (均值差, 下界, 上界)。

    重采样单位是 example，不是 invocation —— 对 invocation 重采样估的是
    "这批样本测得多准"，那不是我们要问的问题（SKILL.md Step 9）。
    """
    diffs = [c - b for b, c in pairs]
    mean = statistics.fmean(diffs)
    rng = random.Random(seed)
    n = len(diffs)
    boots = []
    for _ in range(iterations):
        boots.append(statistics.fmean([diffs[rng.randrange(n)] for _ in range(n)]))
    boots.sort()
    alpha = (1.0 - confidence) / 2.0
    lo = boots[int(alpha * iterations)]
    hi = boots[min(iterations - 1, int((1.0 - alpha) * iterations))]
    return mean, lo, hi


# 延迟合理性上限。超过它的格子判为**测量污染**而非真实慢。
# 依据：本地 agent 正常单次调用 5–20s，云上 9–13s；Bedrock 节流时可能到 1–2 分钟。
# 而实测出现过单格 4,312,942ms（72 分钟）—— 而当时整个 run 才跑了 40 分钟，
# 物理上不可能。根因是 **笔记本睡眠**（`pmset -g log` 确认多次 Clamshell Sleep）：
# 进程被挂起，wall clock 却继续走。见 BUILD-LOG 5.3。
LATENCY_SANITY_CEILING_MS = 300_000


def perf_stats(records: list[dict]) -> dict[str, float]:
    ok = [r for r in records if r.get("status") == "ok"]
    raw = [r["latency_ms"] for r in ok if isinstance(r.get("latency_ms"), (int, float))]
    lat = [v for v in raw if v <= LATENCY_SANITY_CEILING_MS]
    dropped = len(raw) - len(lat)
    # token 来自 replay 时从 trace 抓下来的 `total_tokens`（见 replay.py 的注释）。
    toks = [r["total_tokens"] for r in ok
            if isinstance(r.get("total_tokens"), (int, float))]
    # 用**中位数**而不是均值比较延迟：均值被少数睡眠污染格拉偏，
    # 中位数对 75 格里的几个异常值免疫。这是一次明确的方法学选择，
    # 会写进报告，不藏着。
    return {"latency_median": statistics.median(lat) if lat else 0.0,
            "latency_dropped_outliers": dropped,
            "latency_observed": len(lat),
            "token_mean": statistics.fmean(toks) if toks else 0.0,
            "token_observed": len(toks)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--set", default="control", choices=["control", "holdout"])
    ap.add_argument("--waive-gate", action="append", default=[], metavar="GATE=理由",
                    help="人工豁免某道门，格式 `门名前缀=理由`。豁免会写进 gates-*.json 并在"
                         "摘要里显式标出 —— **绝不静默放行**")
    args = ap.parse_args()

    # 豁免机制的设计意图（重要）：
    #
    # SKILL.md Invariant 3 把"是否上线"留给人，因为 SOP 无法评估爆炸半径。
    # 但人工决定必须**可审计**，否则和偷偷调阈值没有区别。所以这里的做法是：
    #   1. 阈值一个字都不改 —— 门仍然按原值计算、仍然记为 ❌；
    #   2. 豁免只影响"整体是否算通过"，且必须给出理由；
    #   3. 豁免项连同理由写进 gates-*.json，摘要里单独一节列出。
    #
    # 于是事后任何人都能看到：**这个候选没有通过哪道门、是谁、以什么理由放行的**。
    waivers: dict[str, str] = {}
    for w in args.waive_gate:
        if "=" not in w:
            raise SystemExit(f"--waive-gate 需要 `门名前缀=理由` 格式，收到：{w}")
        k, v = w.split("=", 1)
        if not v.strip():
            raise SystemExit(f"豁免 `{k}` 必须给出理由 —— 无理由的豁免就是偷偷调阈值")
        waivers[k.strip()] = v.strip()

    run_dir = REPO / ".omni" / "self-evolution" / args.run
    meta = load_json(run_dir / f"replay-{args.set}.json")
    sem_file = load_json(REPO / "evaluators" / "semantics.json")
    semantics = {k: v for k, v in sem_file["quality_evaluators"].items() if not k.startswith("_")}
    # evaluator_id 在 Phase 2 已解析并冻结进 baseline manifest；这里重新解析一次本地 id
    for name, sem in semantics.items():
        sem.setdefault("evaluator_id", name)
    pg = next((e for e in omni("manage_evaluations", {"action": "list_evaluators",
                                                     "project_path": "."}).get("evaluators", [])
               if "PolicyGrounding" in str(e.get("name", ""))), None)
    if pg and "PolicyGrounding" in semantics:
        semantics["PolicyGrounding"]["evaluator_id"] = pg["id"]

    variants = meta["variants"]
    print(f"run={args.run}  集合={args.set}  变体={variants}")

    arms: dict[str, dict] = {}
    for v in variants:
        recs = [json.loads(l) for l in
                (run_dir / "runs" / v / "invocations.jsonl").read_text().splitlines() if l.strip()]
        print(f"  给 arm `{v}` 打分（{sum(1 for r in recs if r.get('status')=='ok')} 格有效）…")
        arms[v] = {"records": recs, "scores": score_arm(v, recs, semantics, run_dir),
                   "perf": perf_stats(recs)}
        sc = arms[v]["scores"]
        print(f"    → 打到分的 scenario {len(sc)} 个；"
              f"维度覆盖 {sorted({k for d in sc.values() for k in d})}")

    base = arms[BASELINE_ID]
    results = []

    for v in variants:
        if v == BASELINE_ID:
            continue
        cand = arms[v]
        shared = sorted(set(base["scores"]) & set(cand["scores"]))
        pairs = []
        for s in shared:
            b, c = composite(base["scores"][s], semantics), composite(cand["scores"][s], semantics)
            if b is not None and c is not None:
                pairs.append((b, c))

        gates: list[dict] = []

        def add(name: str, value, threshold, passed: bool, note: str = "") -> None:
            gates.append({"gate": name, "value": value, "threshold": threshold,
                          "passed": bool(passed), "note": note})

        if len(pairs) < 2:
            add("配对样本数", len(pairs), "≥2", False,
                "配对样本不足，无法计算区间 —— 按「缺失即失败」处理，不当通过")
            results.append({"variant": v, "gates": gates, "passed": False,
                            "paired_examples": len(pairs)})
            continue

        mean_delta, lo, hi = paired_bootstrap(pairs, BOOTSTRAP_ITERATIONS,
                                              BOOTSTRAP_CONFIDENCE, BOOTSTRAP_SEED)
        add("均值质量提升", round(mean_delta, 4), f"≥{MIN_QUALITY_DELTA}",
            mean_delta >= MIN_QUALITY_DELTA, "低于此值落在判官自身波动内（实测约 0.02）")
        add(f"paired bootstrap {BOOTSTRAP_CONFIDENCE:.0%} 下界",
            round(lo, 4), "≥0", lo >= 0,
            f"区间 [{lo:.4f}, {hi:.4f}]，对样本重采样 {BOOTSTRAP_ITERATIONS} 次")

        # 逐 evaluator 回退
        worst_name, worst = None, 0.0
        for logical, sem in semantics.items():
            if not sem.get("per_example", True):
                continue
            bs = [base["scores"][s][logical] for s in shared
                  if logical in base["scores"].get(s, {}) and logical in cand["scores"].get(s, {})]
            cs = [cand["scores"][s][logical] for s in shared
                  if logical in base["scores"].get(s, {}) and logical in cand["scores"].get(s, {})]
            if not bs:
                continue
            d = statistics.fmean(cs) - statistics.fmean(bs)
            if d < worst:
                worst, worst_name = d, logical
        add("最差单维回退", round(worst, 4), f"≥-{MAX_EVALUATOR_REGRESSION}",
            worst >= -MAX_EVALUATOR_REGRESSION,
            f"最差维度：{worst_name or '无回退'} —— 拦住「靠牺牲一维换平均分」")

        bp, cp = base["perf"], cand["perf"]
        lat_pct = ((cp["latency_median"] - bp["latency_median"]) / bp["latency_median"] * 100
                   if bp["latency_median"] else 0.0)
        dropped = bp["latency_dropped_outliers"] + cp["latency_dropped_outliers"]
        note = "按中位数比较；靠更长推理赢的 prompt，代价不在质量分里"
        if dropped:
            note += f"（剔除 {dropped} 个 >{LATENCY_SANITY_CEILING_MS//1000}s 的测量污染格，见 BUILD-LOG 5.3）"
        add("延迟回退 %（中位数）", round(lat_pct, 2), f"≤{MAX_LATENCY_REGRESSION_PCT}",
            lat_pct <= MAX_LATENCY_REGRESSION_PCT, note)

        if bp["token_observed"] and cp["token_observed"]:
            tok_pct = ((cp["token_mean"] - bp["token_mean"]) / bp["token_mean"] * 100
                       if bp["token_mean"] else 0.0)
            add("token 回退 %", round(tok_pct, 2), f"≤{MAX_TOKEN_REGRESSION_PCT}",
                tok_pct <= MAX_TOKEN_REGRESSION_PCT)
        else:
            # SKILL.md Step 9：缺失的门禁输入按失败处理，不按通过
            add("token 回退 %", "未观测到", f"≤{MAX_TOKEN_REGRESSION_PCT}", False,
                "没有 token 计数 —— 未观测不等于未回退，按「缺失即失败」处理")

        # 标记被豁免的门。阈值与判定本身不动，只记录"这道门被谁以什么理由放行"。
        waived = []
        for g in gates:
            for k, why in waivers.items():
                if not g["passed"] and g["gate"].startswith(k):
                    g["waived_by_human"] = why
                    waived.append(g["gate"])
        blocking = [g for g in gates if not g["passed"] and "waived_by_human" not in g]
        results.append({"variant": v,
                        "paired_examples": len(pairs),
                        "mean_delta": round(mean_delta, 4),
                        "ci": [round(lo, 4), round(hi, 4)],
                        "gates": gates,
                        "waived_gates": waived,
                        "passed_all_gates": all(g["passed"] for g in gates),
                        "passed": not blocking})

    winners = [r for r in results if r["passed"]]

    # SKILL.md Step 11 明确要求**区分**这两种结局：
    #   NO_CHANGE   —— 测量成功了，只是没人赢
    #   NO_DECISION —— 根本没测成（配对样本不足等）
    # 混同会让一次没测成的运行被读成「基线已验证」。
    measurable = [r for r in results if r.get("paired_examples", 0) >= 2]
    # 有豁免时结局另记一档 —— **不允许**把人工放行伪装成"通过了全部门禁"。
    if winners and any(r.get("waived_gates") for r in winners):
        verdict = "WINNER_BY_HUMAN_WAIVER"
    elif winners:
        verdict = "WINNER"
    elif measurable:
        verdict = "NO_CHANGE"
    else:
        verdict = "NO_DECISION"

    (run_dir / f"gates-{args.set}.json").write_text(
        json.dumps({"run": args.run, "set": args.set,
                    "verdict": verdict,
                    "human_waivers": waivers,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "thresholds": {
                        "min_quality_delta": MIN_QUALITY_DELTA,
                        "bootstrap_confidence": BOOTSTRAP_CONFIDENCE,
                        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
                        "bootstrap_seed": BOOTSTRAP_SEED,
                        "max_evaluator_regression": MAX_EVALUATOR_REGRESSION,
                        "max_latency_regression_pct": MAX_LATENCY_REGRESSION_PCT,
                        "max_token_regression_pct": MAX_TOKEN_REGRESSION_PCT},
                    "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'候选':28s} {'配对':>4s} {'Δ质量':>8s} {'95% CI':>20s}  判决")
    for r in results:
        ci = f"[{r['ci'][0]:+.3f},{r['ci'][1]:+.3f}]" if "ci" in r else "—"
        d = f"{r['mean_delta']:+.4f}" if "mean_delta" in r else "—"
        print(f"{r['variant']:28s} {r['paired_examples']:>4d} {d:>8s} {ci:>20s}  "
              f"{'✅ 全部通过' if r['passed'] else '❌ 未通过'}")
        for g in r["gates"]:
            if not g["passed"]:
                tag = "⚖️ 已人工豁免" if "waived_by_human" in g else "✗"
                print(f"    {tag} {g['gate']}: {g['value']}（门槛 {g['threshold']}）"
                      f"{' —— ' + g['note'] if g['note'] else ''}")
                if "waived_by_human" in g:
                    print(f"        豁免理由：{g['waived_by_human']}")

    print(f"\n通过全部门禁的候选：{[r['variant'] for r in results if r.get('passed_all_gates')] or '无'}")
    print(f"经人工豁免后判为通过的候选：{[r['variant'] for r in winners] or '无'}")
    print(f"→ 结局：`{verdict}`")
    if verdict == "WINNER_BY_HUMAN_WAIVER":
        print("  ⚠️ 这不是「通过了全部门禁」。有门被人工豁免，上面已逐条列出理由。")
        print("  报告与 decision.json 必须保留这个区分 —— 否则事后会被读成自动通过。")
    if verdict == "NO_CHANGE":
        print("  候选跑了、也测了，但没有一个赢过 baseline。")
        print("  这是一次**成功**的运行 —— 它确立了这些假设打不过基线。")
    elif verdict == "NO_DECISION":
        print("  ⚠️ **没有产生有效测量**（配对样本不足）。这不等于「基线更好」——")
        print("  它意味着这次实验什么都没证明。不要据此下任何质量结论。")
    print(f"✓ .omni/self-evolution/{args.run}/gates-{args.set}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
