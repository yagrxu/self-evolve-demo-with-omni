#!/usr/bin/env python3
"""把人工决定的验收标准施加到**已算好**的门禁值上（不重新打分、不改原门禁文件）。

SKILL.md 规定看到结果后不得调整阈值。人可以做出不同的决定，但必须可审计：
原 gates-<set>.json 一字不改；本脚本另写 gates-<set>-human.json，逐门记录
原阈值、人工阈值、谁、何时、理由；结局记为 WINNER_BY_HUMAN_WAIVER，与 WINNER 区分。

    python scripts/se_human_waiver.py --run <id> --set control \
        --rule "bootstrap 95% 下界=-0.01" --rule "token 均值回退 %=15" --reason "…"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOWER_IS_THRESHOLD = {"bootstrap 95% 下界", "均值质量提升", "最差单维回退"}  # value ≥ 阈值


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--set", default="control")
    ap.add_argument("--rule", action="append", required=True, metavar="门名=人工阈值")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--by", default="user")
    a = ap.parse_args()
    run_dir = REPO / ".omni" / "self-evolution" / a.run
    g = json.loads((run_dir / f"gates-{a.set}.json").read_text())
    rules = {k.strip(): float(v) for k, v in (r.split("=", 1) for r in a.rule)}

    results = []
    for r in g["results"]:
        gates = []
        for x in r["gates"]:
            y = dict(x)
            if x["gate"] in rules and isinstance(x["value"], (int, float)):
                thr = rules[x["gate"]]
                ok = x["value"] >= thr if x["gate"] in LOWER_IS_THRESHOLD else x["value"] <= thr
                y.update({"original_threshold": x["threshold"], "original_passed": x["passed"],
                          "human_threshold": thr, "passed_under_human_threshold": ok})
            gates.append(y)
        failed_orig = [x["gate"] for x in gates if not x["passed"]]
        waived = [x["gate"] for x in gates if not x["passed"] and x.get("passed_under_human_threshold")]
        blocking = [x["gate"] for x in gates if not x["passed"] and not x.get("passed_under_human_threshold")]
        results.append({"variant": r["variant"], "mean_delta": r.get("mean_delta"), "ci": r.get("ci"),
                        "failed_original_gates": failed_orig, "waived_gates": waived,
                        "still_blocking": blocking, "passed_with_waiver": not blocking, "gates": gates})
    winners = [r for r in results if r["passed_with_waiver"]]
    leader = max(winners, key=lambda r: r["mean_delta"])["variant"] if winners else None
    out = {"run": a.run, "set": a.set, "source": f"gates-{a.set}.json（未修改）",
           "verdict": "WINNER_BY_HUMAN_WAIVER" if winners else g["verdict"],
           "original_verdict": g["verdict"], "leader": leader,
           "human_rules": rules, "reason": a.reason, "by": a.by,
           "decided_at": datetime.now(timezone.utc).isoformat(),
           "decided_after_seeing_results": True, "results": results}
    (run_dir / f"gates-{a.set}-human.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    for r in results:
        print(f"{r['variant']:26s} 原未过 {r['failed_original_gates'] or '—'}  人工放行 {r['waived_gates'] or '—'}  "
              f"仍拦截 {r['still_blocking'] or '—'}  → {'✅' if r['passed_with_waiver'] else '❌'}")
    print(f"\n原结局 {g['verdict']}  →  人工结局 {out['verdict']}" + (f"（{leader}）" if leader else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
