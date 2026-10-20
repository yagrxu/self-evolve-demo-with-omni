#!/usr/bin/env python3
"""SKILL.md Step 3 —— 用 Omni ``search_agent_traces`` 只读拉取生产 trace。

与 ``export_bad_traces.py`` 的区别：那份走 boto3 直读评估 log group（当时 Omni 云端
不可用，决策点 R8）。这份走 SKILL.md 规定的正路：``discover_agent_traces_metadata``
→ ``search_agent_traces``，把每个 session 的**全部 span** 原样写进
``<run_dir>/traces/raw.jsonl``。

实测的列名事实（2026-10-19，us-west-2 default store）：
  * 扁平列 ``"attributes.session.id"`` 在本 store 里永远是空串 —— 必须用
    ``attributes['session.id']`` 这种 map 访问。用错不会报错，只会 0 行。
  * agent 名在 ``attributes['gen_ai.agent.name']``（Strands 写的 semconv 名），
    ``attributes['agent.name']`` 为空。

只读：只调用 search_agent_traces（SELECT）。

用法：
    python scripts/se_harvest.py --run <run_id> [--hours 24]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omni_client import omni  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCOPE = "\"@telemetry_type\" = 'traces'"


def sql(queries: list[str]) -> list[dict]:
    res = omni("search_agent_traces", {"queries": queries}, timeout_s=300)
    out = res.get("results") or []
    for r in out:
        if r.get("error"):
            raise SystemExit(f"查询失败：{r['error']}\n{r.get('query')}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()

    run_dir = REPO / ".omni" / "self-evolution" / args.run
    manifest = json.loads((run_dir / "manifest.json").read_text())
    agent = manifest["agent_name"].replace("'", "''")

    end = int(time.time())
    start = end - args.hours * 3600
    win = f"\"@timestamp\" BETWEEN to_timestamp({start}) AND to_timestamp({end})"

    # 1) 列出该 agent 在窗口内的 session（只取 invoke_agent 根 span，不碰 payload 列）
    q_sessions = (
        f"SELECT attributes['session.id'] AS session_id, traceId AS trace_id, "
        f"MIN(\"@timestamp\") AS ts FROM \"default\" "
        f"WHERE ({win} AND attributes['gen_ai.agent.name'] = '{agent}') AND {SCOPE} "
        f"GROUP BY 1, 2 ORDER BY 3 LIMIT 500")
    rows = sql([q_sessions])[0]["rows"]
    sessions: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("session_id"):
            sessions[r["session_id"]].add(r["trace_id"])
    print(f"窗口 {start}..{end}（{args.hours}h）  agent='{manifest['agent_name']}'  "
          f"session {len(sessions)} 个 / trace {sum(len(v) for v in sessions.values())} 条")
    if not sessions:
        print("0 行 —— 按 SKILL.md 先当作查错（列名/region/时间单位），不当作没流量")
        return 2

    # 2) 逐 session 下钻全部 span（小 LIMIT，按 session 过滤；15 条一批）
    out_dir = run_dir / "traces"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "raw.jsonl"
    n_spans = 0
    sids = sorted(sessions)
    with raw_path.open("w", encoding="utf-8") as fh:
        for i in range(0, len(sids), 15):
            batch = sids[i:i + 15]
            qs = [(f"SELECT \"@record\" AS rec FROM \"default\" "
                   f"WHERE ({win} AND attributes['session.id'] = '{s}') AND {SCOPE} LIMIT 200")
                  for s in batch]
            for s, res in zip(batch, sql(qs)):
                spans = [json.loads(r["rec"]) for r in res["rows"]]
                n_spans += len(spans)
                fh.write(json.dumps({"session_id": s, "trace_ids": sorted(sessions[s]),
                                     "span_count": len(spans), "spans": spans},
                                    ensure_ascii=False) + "\n")
    meta = {"window_start_s": start, "window_end_s": end, "hours": args.hours,
            "agent_filter": "attributes['gen_ai.agent.name'] = " + repr(manifest["agent_name"]),
            "sessions": len(sessions), "spans": n_spans, "query_sessions": q_sessions}
    (out_dir / "harvest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"✓ {raw_path.relative_to(REPO)}：{len(sessions)} session / {n_spans} span")
    return 0


if __name__ == "__main__":
    sys.exit(main())
