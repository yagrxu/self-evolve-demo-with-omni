#!/usr/bin/env python3
"""SKILL.md Step 6 —— 构建 eligible 集并按 scenario_id 稳定哈希切分（候选生成之前）。

    python scripts/se_step6.py --run <run_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIN_ELIGIBLE, MIN_CONTROL, MIN_HOLDOUT, HOLDOUT_FRACTION = 12, 5, 5, 0.30


def stable_bucket(scenario_id: str) -> float:
    # 与 export_bad_traces.stable_bucket 完全相同：blake2b，纯函数，跨进程稳定
    h = hashlib.blake2b(scenario_id.encode("utf-8"), digest_size=8).hexdigest()
    return int(h, 16) / float(1 << 64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = REPO / ".omni" / "self-evolution" / args.run
    # 样本来源 = Step 3 拉回的生产 trace（session → scenario 由流量 manifest 确定）
    raw = {}
    for line in (run_dir / "traces" / "raw.jsonl").read_text().splitlines():
        r = json.loads(line)
        raw[r["session_id"]] = r
    prod = json.loads((run_dir / "production_eval" / "scores.json").read_text())
    scores = {}
    for sess, rec in prod.items():
        r = raw.get(sess)
        if not r:
            continue
        has_output = any(e.get("name") == "gen_ai.choice"
                         for s in r["spans"] if s["name"].startswith("invoke_agent")
                         for e in s.get("events") or [])
        if has_output:
            scores[r["trace_ids"][0]] = {"scenario_id": rec["scenario_id"], "session_id": sess,
                                         "dataset": sess.split("-")[1]}
    ex = {}
    for ds in ("prod_sim", "dev_local"):
        for e in json.loads((REPO / "datasets" / f"{ds}.json").read_text())["examples"]:
            ex[e["scenario_id"]] = e

    eligible = []
    for tid, rec in sorted(scores.items(), key=lambda kv: kv[1]["scenario_id"]):
        e = ex.get(rec["scenario_id"])
        if not e or not e["turns"][0]["input"]:
            continue
        eligible.append({**e, "metadata": {
            **e["metadata"], "source_trace_id": tid, "source_session_id": rec["session_id"],
            "source_dataset": rec["dataset"],
            # 期望轨迹按 **baseline 的工具层**（basic）记录 —— SKILL.md Step 6 最后一条
            "expected_trajectory_tier": "basic"}})

    holdout = [e for e in eligible if stable_bucket(e["scenario_id"]) < HOLDOUT_FRACTION]
    control = [e for e in eligible if stable_bucket(e["scenario_id"]) >= HOLDOUT_FRACTION]
    d = run_dir / "datasets"
    d.mkdir(exist_ok=True)
    for name, data in (("eligible", eligible), ("control", control), ("holdout", holdout)):
        (d / f"{name}.json").write_text(json.dumps(
            {"name": name, "count": len(data), "examples": data}, ensure_ascii=False, indent=2))
    split = {"function": "blake2b(scenario_id, digest_size=8) / 2^64 < 0.30 → holdout",
             "eligible": len(eligible), "control": len(control), "holdout": len(holdout),
             "holdout_ids": sorted(e["scenario_id"] for e in holdout),
             "control_sha256": hashlib.sha256((d / "control.json").read_bytes()).hexdigest(),
             "holdout_sha256": hashlib.sha256((d / "holdout.json").read_bytes()).hexdigest()}
    (d / "split.json").write_text(json.dumps(split, ensure_ascii=False, indent=2))
    blockers = [m for m, ok in (
        (f"eligible {len(eligible)} < {MIN_ELIGIBLE}", len(eligible) >= MIN_ELIGIBLE),
        (f"control {len(control)} < {MIN_CONTROL}", len(control) >= MIN_CONTROL),
        (f"holdout {len(holdout)} < {MIN_HOLDOUT}", len(holdout) >= MIN_HOLDOUT)) if not ok]
    print(json.dumps({k: v for k, v in split.items() if k != "holdout_ids"}, ensure_ascii=False))
    print("判定：", "NO_DECISION " + "；".join(blockers) if blockers else "READY")
    return 2 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
