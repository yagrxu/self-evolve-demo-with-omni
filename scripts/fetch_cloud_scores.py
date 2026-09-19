#!/usr/bin/env python3
"""Phase 3 收尾 —— 把云端在线评估的分数拉下来，产出 ``reports/02-cloud-scores.md``。

为什么不走 Omni 的 ``manage_evaluations(results)``
--------------------------------------------------
两个原因，第二个是决定性的：

1. 那个动作只**读取已经发出的 evaluation span**，不触发评估 —— 我们要的正是它，
   但它依赖 Omni 的云端 endpoint。
2. 而云端 endpoint 目前不可用（见 docs/BUILD-LOG.md 决策点 R8：扩展宿主进程的 env 里
   没有 AWS_REGION，`mcp-proxy.js` 又不转发 env）。

所以这里直接用 boto3 读 CloudWatch Logs。**这不是绕路** —— 在线评估的结果本来就落在
``/aws/bedrock-agentcore/evaluations/results/<configId>``，直连是最短路径，
也让本脚本能在纯 shell / CI 里跑，不依赖 IDE 会话。

对齐方式
--------
靠 manifest 里的 ``session_to_scenario`` 显式映射，**绝不用位置或时间顺序**。
云端评估是异步的、乱序返回的，按顺序对齐必然错位，而错位后的分数看起来完全正常。

用法：
    python scripts/fetch_cloud_scores.py --run traffic-prod_sim-0be7e8bc
    python scripts/fetch_cloud_scores.py --run <run> --wait 900   # 等最多 15 分钟
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
EVAL_LOG_PREFIX = "/aws/bedrock-agentcore/evaluations/results/"


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def canonical(score: float, sem: dict) -> float:
    """归一化到 [0,1]，方向按 semantics.json 的声明处理。

    与 local_baseline.py 里的同名函数保持一致 —— 两边必须是同一把尺子，
    否则本地基线和云上分数不可比，Phase 6 的验证就失去意义。
    """
    lo, hi = float(sem["min"]), float(sem["max"])
    if hi == lo:
        raise ValueError(f"evaluator 值域非法: min==max=={lo}")
    x = max(0.0, min(1.0, (float(score) - lo) / (hi - lo)))
    return x if sem["direction"] == "HIGHER_IS_BETTER" else 1.0 - x


def discover_eval_log_group(logs, region: str) -> str | None:
    """找到在线评估结果的 log group。

    刻意用前缀发现而不是把名字写死：log group 名里含 online-eval config 的随机后缀，
    重新部署 eval stack 会换一个。写死会在下次部署后静默查到一个空的旧 group。
    """
    paginator = logs.get_paginator("describe_log_groups")
    found = []
    for page in paginator.paginate(logGroupNamePrefix=EVAL_LOG_PREFIX):
        for lg in page.get("logGroups", []):
            found.append(lg["logGroupName"])
    if not found:
        return None
    if len(found) > 1:
        print(f"  ⚠️  发现 {len(found)} 个评估结果 log group，取最新的：")
        for f in found:
            print(f"       {f}")
    return sorted(found)[-1]


def fetch_events(logs, log_group: str, start_ms: int, end_ms: int) -> list[dict]:
    """拉时间窗内的全部评估结果事件。"""
    events, token = [], None
    while True:
        kw = {"logGroupName": log_group, "startTime": start_ms, "endTime": end_ms, "limit": 1000}
        if token:
            kw["nextToken"] = token
        resp = logs.filter_log_events(**kw)
        events.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            return events


EVAL_NAME_ATTR = "gen_ai.evaluation.name"
EVAL_SCORE_ATTR = "gen_ai.evaluation.score.value"
EVAL_EXPL_ATTR = "gen_ai.evaluation.explanation"
SESSION_ATTR = "session.id"


def parse_records(events: list[dict]) -> list[dict]:
    """把日志事件解析成扁平的评分记录。

    两个必须处理的实测事实（BUILD-LOG 3.2）：

    1. **一条 log event 的 message 里可能有多个 NDJSON 记录**（换行分隔）。
       用 `json.loads(message)` 只会拿到 `JSONDecodeError: Extra data` 或第一条 ——
       第一版就是这样，导致 20 条事件只解析出个别记录、评分表整列为空。
    2. 记录是 **OTel 日志记录**形态，评分放在扁平的点号属性里：
       `attributes["gen_ai.evaluation.name"]` / `["gen_ai.evaluation.score.value"]`，
       而不是 `{"evaluatorId":..., "score":...}` 这种嵌套结构。

    解析不出的一律计数上报，**不静默丢弃** —— 丢弃会让 coverage 虚高
    （"20/20 都评了"），那比直接报错危险得多。
    """
    out, unparsed = [], 0
    for e in events:
        for line in (e.get("message") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                unparsed += 1
                continue
            out.append({"raw": d, "ts": e.get("timestamp")})
    if unparsed:
        print(f"  ⚠️  {unparsed} 行不是合法 JSON，未解析（已上报，不当成 0 分）")
    return out


def record_session(rec: dict) -> str | None:
    """取这条评分记录所属的 session id（我们在流量里传进去的那个）。"""
    attrs = rec["raw"].get("attributes") or {}
    v = attrs.get(SESSION_ATTR)
    return v if isinstance(v, str) and v else None


def find_scores(rec: dict) -> list[tuple[str, float]]:
    """从一条记录里取出 (evaluatorId, score)。

    一条 `gen_ai.evaluation.result` 记录只承载**一个** evaluator 的结果，
    所以返回值最多一个元素 —— 保持 list 是为了让调用方对"这条没有分数"
    （例如评估失败的记录）无需特殊分支。
    """
    attrs = rec["raw"].get("attributes") or {}
    name, score = attrs.get(EVAL_NAME_ATTR), attrs.get(EVAL_SCORE_ATTR)
    if not isinstance(name, str) or not name:
        return []
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return []
    return [(name, float(score))]


def logical_name(evaluator_id: str, semantics: dict) -> str | None:
    """把云端返回的 evaluator id 映射回 semantics.json 里的逻辑名。

    自定义 evaluator 的云端 id 带随机后缀（selfevolve_demo_PolicyGrounding-IS7lH859gB），
    所以不能靠等号匹配。
    """
    if evaluator_id in semantics:
        return evaluator_id
    for name in semantics:
        short = name.split(".")[-1]
        if short and short in evaluator_id:
            return name
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="runs/ 下的流量 run 目录名")
    ap.add_argument("--wait", type=int, default=0, help="评分未出现时最多等待多少秒（轮询）")
    ap.add_argument("--poll", type=int, default=60, help="轮询间隔秒")
    ap.add_argument("--min-sessions", type=int, default=0,
                    help="至少覆盖多少个 session 才算等到（默认全部）")
    ap.add_argument("--slack", type=int, default=1800,
                    help="查询窗口在流量窗口后额外延展多少秒（在线评估是异步的，会晚于流量落库）")
    args = ap.parse_args()

    run_dir = REPO / "runs" / args.run
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"找不到 {manifest_path}")
    mf = load_json(manifest_path)
    sem_file = load_json(REPO / "evaluators" / "semantics.json")
    semantics = {k: v for k, v in sem_file["quality_evaluators"].items() if not k.startswith("_")}

    region = mf.get("region", "us-west-2")
    s2s: dict[str, str] = mf.get("session_to_scenario", {})
    if not s2s:
        raise SystemExit("manifest 里没有 session_to_scenario —— 无法显式对齐，拒绝按顺序猜")

    logs = boto3.client("logs", region_name=region)
    print(f"run={args.run}  region={region}  会话数={len(s2s)}")

    log_group = discover_eval_log_group(logs, region)
    if not log_group:
        raise SystemExit(
            f"在 {region} 找不到任何 {EVAL_LOG_PREFIX}* log group。\n"
            f"确认 eval stack 已部署且 OnlineEvaluationConfig 的 ExecutionStatus 是 ENABLED。")
    print(f"评估结果 log group：{log_group}")

    start_ms = int(mf["window_start_epoch_s"]) * 1000
    end_ms = (int(mf["window_end_epoch_s"]) + args.slack) * 1000

    # 等待条件是**覆盖到的 session 数**，不是"有没有事件"。
    # 实测（BUILD-LOG 3.2）：在线评估是逐步推进的 —— 20 条流量发完 20 分钟后，
    # 云端只评完了 2 个 session。"有事件就返回"会产出一份覆盖率 1/20 的报告，
    # 而那份报告的均值毫无统计意义，却看起来像正常结果。
    target = args.min_sessions if args.min_sessions > 0 else len(s2s)
    deadline = time.time() + args.wait
    events: list[dict] = []
    while True:
        events = fetch_events(logs, log_group, start_ms, int(time.time() * 1000))
        covered = {sid for r in parse_records(events)
                   if (sid := record_session(r)) is not None and sid in s2s}
        if len(covered) >= target or time.time() >= deadline:
            if len(covered) < target:
                print(f"  ⚠️  等待超时：只覆盖 {len(covered)}/{target} 个 session。"
                      f"报告会如实反映覆盖率，请勿据此下质量结论。")
            break
        left = int(deadline - time.time())
        print(f"  已覆盖 {len(covered)}/{target} 个 session，{args.poll}s 后重试（剩余 {left}s）…")
        time.sleep(min(args.poll, max(1, left)))

    print(f"拉到 {len(events)} 条评估结果事件")
    if not events:
        raise SystemExit(
            "评估结果为空。在线评估是异步的，通常需要几分钟到十几分钟。\n"
            "可以稍后加 --wait 900 重试。\n"
            "⚠️ 空结果**不等于**分数为 0 —— 不要据此得出任何质量结论。")

    records = parse_records(events)

    # scenario_id -> logical evaluator -> [canonical scores]
    by_scenario: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    unmatched_session, unknown_evaluator = 0, set()
    matched_sessions: set[str] = set()

    for rec in records:
        # 显式读 `session.id` 属性，不做整段 payload 的字符串搜索 ——
        # 搜索会把出现在 explanation 正文里的 session 名误判为归属，
        # 而那种误配没有任何征兆。
        sid = record_session(rec)
        if sid is None or sid not in s2s:
            unmatched_session += 1
            continue
        matched_sessions.add(sid)
        scenario = s2s[sid]
        for ev_id, raw_score in find_scores(rec):
            logical = logical_name(ev_id, semantics)
            if logical is None:
                unknown_evaluator.add(ev_id)
                continue
            try:
                by_scenario[scenario][logical].append(canonical(raw_score, semantics[logical]))
            except (TypeError, ValueError):
                pass

    if unmatched_session:
        print(f"  ⚠️  {unmatched_session} 条记录关联不到本 run 的任何 session —— "
              f"可能属于别的 run，已跳过（未当成 0 分）")
    if unknown_evaluator:
        print(f"  ⚠️  {len(unknown_evaluator)} 个 evaluator 不在 semantics.json 中，已跳过："
              f"{sorted(unknown_evaluator)}")

    # 云端每个请求都有独立的 runtimeSessionId，所以 SESSION 级 evaluator 在云上
    # **可能是有效的** —— 与本地 dev server 的情况不同（见决策点 R10：本地 collector
    # 的 session.id 只在启动时设一次，导致 15 条共享一个 session）。
    # 这里实测记录下来，不沿用本地的排除结论。
    distinct_sessions = len(matched_sessions)
    session_level_valid = distinct_sessions >= 2

    lines: list[str] = []
    a = lines.append
    a("# Phase 3 — 云上在线评估分数\n")
    a(f"- 流量 run：`{args.run}`（dataset `{mf['dataset']}`，endpoint `{mf['endpoint']}`）")
    a(f"- runtime：`{mf['runtime_arn'].split('/')[-1]}`  ·  region `{region}`")
    a(f"- 评估结果 log group：`{log_group}`")
    a(f"- 流量窗口：{mf['started_at']} .. {mf['finished_at']}")
    a(f"- 生成时间：{datetime.now(timezone.utc).isoformat()}")
    a(f"- 匹配到评分的 session：{distinct_sessions} / {len(s2s)}\n")

    a("## 每个 evaluator 的云上均值（已归一化到 0–1，越高越好）\n")
    a("| Evaluator | 层级 | 覆盖场景 | 均值 | 及格率 |")
    a("|---|---|---:|---:|---:|")
    for logical, sem in semantics.items():
        vals = [v for sc in by_scenario.values() for v in sc.get(logical, [])]
        n_scenarios = sum(1 for sc in by_scenario.values() if sc.get(logical))
        if not vals:
            a(f"| `{logical}` | {sem['level']} | 0/{len(s2s)} | — | — |")
            continue
        pass_canon = canonical(float(sem["pass_threshold"]), sem)
        rate = 100 * sum(1 for v in vals if v >= pass_canon) / len(vals)
        mark = "" if sem.get("per_example", True) or session_level_valid else " ⚠️"
        a(f"| `{logical}`{mark} | {sem['level']} | {n_scenarios}/{len(s2s)} | "
          f"{sum(vals)/len(vals):.3f} | {rate:.0f}% |")
    a("")
    a("> 未评分一律不计入均值，**绝不当 0 分** —— 把缺失当 0 会凭空造出「质量很差」的结论。\n")

    if session_level_valid:
        a(f"> ℹ️ 本 run 匹配到 **{distinct_sessions} 个不同 session**，所以 SESSION 级 evaluator "
          f"在云上是**有效**的 —— 与本地不同（决策点 R10：本地 dev server 的 collector "
          f"`session.id` 只在启动时设一次，15 条调用共享一个 session，导致 n=1 伪装成 n=15）。"
          f"云端每个请求带独立的 `runtimeSessionId`，因此这里不沿用本地的排除结论。\n")
    else:
        a(f"> ⚠️ 只匹配到 {distinct_sessions} 个 session，SESSION 级 evaluator 的结果仍不可当作"
          f"多个独立观测（同 R10）。\n")

    a("## 逐场景明细\n")
    ordered = sorted(by_scenario)
    cols = [k for k in semantics if any(by_scenario[s].get(k) for s in ordered)]
    a("| scenario | " + " | ".join(k.split(".")[-1] for k in cols) + " |")
    a("|---" * (len(cols) + 1) + "|")
    for s in ordered:
        cells = []
        for k in cols:
            vs = by_scenario[s].get(k, [])
            cells.append(f"{sum(vs)/len(vs):.2f}" if vs else "—")
        a(f"| `{s}` | " + " | ".join(cells) + " |")
    a("")

    out_json = run_dir / "cloud-scores.json"
    out_json.write_text(json.dumps(
        {"run": args.run, "log_group": log_group, "distinct_sessions": distinct_sessions,
         "session_level_valid": session_level_valid,
         "scores": {s: dict(v) for s, v in by_scenario.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")

    a("## 产物\n")
    a(f"- `{out_json.relative_to(REPO)}` —— 归一化后的逐场景分数")
    a(f"- `{(run_dir / 'traffic.jsonl').relative_to(REPO)}` —— 每条请求的输入/输出/echo")
    a("")
    a("## 下一步\n")
    a("Phase 4：从这些云端 trace 里挑出差的，标注并导出到本地，作为 Phase 5 的合格样本集。")

    report = REPO / "reports" / "02-cloud-scores.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✓ 报告：{report.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
