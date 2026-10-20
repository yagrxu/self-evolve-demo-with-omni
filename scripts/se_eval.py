"""用 run 目录里**冻结**的 evaluators.json 给一批 targets 打分。

Step 5（生产 trace）与 Step 9/10（本地 replay）共用这一个函数 —— 尺子只有一把。
evaluators.json 在 Step 4 之后只读；这里每次都校验它的 sha256 与 manifest 一致，
被改过就直接拒绝（SKILL.md Invariant 4）。
"""
from __future__ import annotations

import hashlib
import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from omni_client import OmniError, omni

BATCH = 15


def load_frozen(run_dir: Path) -> dict:
    p = run_dir / "evaluators.json"
    want = json.loads((run_dir / "manifest.json").read_text())["evaluators_sha256"]
    got = hashlib.sha256(p.read_bytes()).hexdigest()
    if got != want:
        raise SystemExit(f"evaluators.json 被改过（{got[:12]} ≠ manifest {want[:12]}）—— "
                         f"违反 Invariant 4，拒绝打分")
    return json.loads(p.read_text())


def canonical(raw: float, sem: dict) -> float:
    lo, hi = float(sem["min"]), float(sem["max"])
    x = max(0.0, min(1.0, (float(raw) - lo) / (hi - lo)))
    return x if sem["direction"] == "HIGHER_IS_BETTER" else 1.0 - x


def _run_one(ev_id: str, batch: list[dict]) -> list[dict]:
    for attempt in range(3):
        try:
            res = omni("manage_evaluations", {"action": "run", "project_path": ".",
                                              "evaluatorId": ev_id, "targets": batch},
                       timeout_s=1800)
            return res.get("results") or []
        except OmniError as e:
            if attempt == 2:
                print(f"    ⚠️ {ev_id} 一批 {len(batch)} 条评估失败：{e}")
    return []


def score_targets(targets: list[dict], frozen: dict, workers: int = 6
                  ) -> dict[str, dict[str, dict]]:
    """返回 {key: {logical_name: {"raw":…, "canonical":…, "explanation":…}}}。

    -1 / None / 有 error 一律记为**未评分**，绝不当 0 分。
    """
    jobs = []
    for logical, sem in frozen["evaluators"].items():
        for i in range(0, len(targets), BATCH):
            jobs.append((logical, sem, targets[i:i + BATCH]))
    out: dict[str, dict[str, dict]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [(lg, sem, pool.submit(_run_one, sem["id"], b)) for lg, sem, b in jobs]
        for logical, sem, fut in futs:
            for row in fut.result():
                key, raw = row.get("itemKey"), row.get("score")
                if key is None or raw is None or raw == -1 or row.get("error"):
                    continue
                out.setdefault(key, {})[logical] = {
                    "raw": raw, "canonical": canonical(raw, sem),
                    "explanation": (row.get("explanation") or "")[:1200]}
    return out


def composite(per_eval: dict[str, float], frozen: dict) -> float | None:
    vals = [v for k, v in per_eval.items() if frozen["evaluators"][k]["role"] == "quality"]
    n_quality = sum(1 for e in frozen["evaluators"].values() if e["role"] == "quality")
    # 缺维度的样本不进综合分：少一维的均值和满维的均值不可比
    return statistics.fmean(vals) if len(vals) == n_quality else None
