#!/usr/bin/env python3
"""Phase 2：本地基线 —— 注册 dataset、建自定义 evaluator、跑完 15 条、多维打分。

产出 ``reports/01-local-baseline.md``。预期结论是"**分数不好看**"——
这正是整个 Demo 的起点，后面 Phase 5 的提升才有对照。

关于"哪条 trace 对应哪条标注答案"
--------------------------------
skill Step 8.3 明令禁止靠位置匹配（positional dataset matching）。
这里的做法是**先 invoke、再把拿到的 traceId 写回 dataset example 的
``metadata.sourceTraceId``**，然后评估时 `manage_evaluations` 就能按
sourceTraceId 显式对齐，而不是"第 3 条对第 3 条"。

用法：
    python scripts/local_baseline.py                  # 全量 15 条
    python scripts/local_baseline.py --limit 3        # 先跑 3 条验证链路
    python scripts/local_baseline.py --skip-invoke    # 复用上次的 trace，只重跑评估
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omni_client import OmniError, omni, require_omni  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DATASET_NAME = "dev_local"
PROMPT_NAME = "order_support"
# 本地 dev server 的启动命令。BedrockAgentCoreApp 默认监听 8080。
#
# **必须带 opentelemetry-instrument**，与云端 entryPoint 保持一致。
# 实测（BUILD-LOG Phase 1.14）：裸 `python agent.py` 时 TracerProvider 是
# ProxyTracerProvider，OmniPromptProcessor 挂不上去，trace 上就没有
# llm.prompt_template.version —— 而 skill 要求那个属性缺失即 NO_DECISION。
# Omni 在托管模式下会注入 PYTHONPATH 达到同样效果，但显式写出来更可靠、
# 也让本地与云端跑的是同一条埋点路径。
START_COMMAND = "cd agent && ../.venv/bin/opentelemetry-instrument ../.venv/bin/python agent.py"
DEV_PORT = 8080


# ── 工具函数 ───────────────────────────────────────────────────────────────


def load_json(rel: str) -> dict:
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


def canonical(score: float, sem: dict) -> float:
    """把原始分归一化到 [0,1]，方向已按 semantics.json 的声明处理。

    skill Step 8.6 的规则：HIGHER_IS_BETTER 用 (s-min)/(max-min)，
    LOWER_IS_BETTER 用 1-(s-min)/(max-min)。方向搞反会把更差的候选选成 winner，
    所以这一步单独成函数并在下面有断言覆盖。
    """
    lo, hi = float(sem["min"]), float(sem["max"])
    if hi == lo:
        raise ValueError(f"evaluator 值域非法: min==max=={lo}")
    x = (float(score) - lo) / (hi - lo)
    x = max(0.0, min(1.0, x))
    return x if sem["direction"] == "HIGHER_IS_BETTER" else 1.0 - x


# ── Step 1/2：配置并启动本地 agent ────────────────────────────────────────


def configure_agent() -> None:
    """把启动命令和协议 schema 存进 Omni。

    注意数据流方向（skill 反复强调过）：**是我们告诉 Omni，不是 Omni 告诉我们。**
    `configure_omni(get_omni_config)` 返回的是上次保存的结果，不是"应该是什么"。
    这些值来自读代码，那是我们的职责。
    """
    omni("configure_omni", {
        "action": "set_project_details",
        "project_path": ".", "framework": "strands", "language": "python", "platform": "agentcore",
    })
    omni("configure_omni", {
        "action": "prompt_setup_complete",
        "project_path": ".", "promptsJsonPath": "agent/prompts.json",
    })
    omni("manage_test_agent", {
        "action": "set_start_command",
        "project_path": ".", "command": START_COMMAND, "port": DEV_PORT,
    })
    # BedrockAgentCoreApp 的协议就是 Omni 内置的 AgentCore preset：
    # POST /invocations，body {"prompt": ..., "sessionId": ...}
    omni("manage_test_agent", {"action": "set_agent_schema", "project_path": ".", "preset": "AgentCore"})
    print("✓ 已保存 start command 与 AgentCore schema")


def ensure_server() -> None:
    """确保 collector 和 dev server 都在跑。"""
    collector = omni("manage_local_collector", {"action": "start", "project_path": "."})
    print(f"✓ collector: {json.dumps(collector, ensure_ascii=False)}")

    ping = omni("manage_test_agent", {"action": "ping", "project_path": "."})
    if ping.get("active"):
        print(f"✓ agent 已在运行: {ping.get('endpoint')}")
        return

    print(f"agent 未运行（{ping.get('reason')}），启动中…")
    omni("local_server", {"action": "start"})
    for attempt in range(1, 13):  # 最多等 60s
        time.sleep(5)
        ping = omni("manage_test_agent", {"action": "ping", "project_path": "."})
        if ping.get("active"):
            print(f"✓ agent 启动成功（第 {attempt} 次探测）: {ping.get('endpoint')}")
            return
    out = omni("local_server", {"action": "get_output", "lines": 60})
    raise SystemExit(f"dev server 起不来。终端输出：\n{json.dumps(out, ensure_ascii=False, indent=2)}")


# ── Step 3：注册 dataset ──────────────────────────────────────────────────


def ensure_dataset(examples: list[dict], description: str) -> str:
    """建（或复用）本地 dataset，返回 datasetId。"""
    existing = omni("manage_datasets", {"action": "list", "dataSource": "local", "project_path": "."})
    for d in (existing.get("datasets") or existing if isinstance(existing, list) else existing.get("datasets", [])):
        if isinstance(d, dict) and d.get("name") == DATASET_NAME:
            print(f"✓ 复用已有 dataset {DATASET_NAME} ({d.get('id') or d.get('datasetId')})")
            return d.get("id") or d["datasetId"]

    created = omni("manage_datasets", {
        "action": "create", "dataSource": "local", "project_path": ".",
        "name": DATASET_NAME, "description": description, "examples": examples,
    })
    ds_id = created.get("id") or created.get("datasetId")
    print(f"✓ 新建 dataset {DATASET_NAME} ({ds_id})，{len(examples)} 条 example")
    return ds_id


def freeze_dataset_version(ds_id: str) -> int | None:
    """冻结一个不可变版本并读回校验。

    skill Step 5.5：必须 create version 然后**读回**核对条数与内容哈希 ——
    不核对的话，dataset 在评估中途被改了也发现不了，A/B 就不可比。
    """
    ver = omni("manage_datasets", {
        "action": "create_version", "dataSource": "local", "project_path": ".", "datasetId": ds_id,
    })
    version = ver.get("version")
    readback = omni("manage_datasets", {
        "action": "get_examples", "dataSource": "local", "project_path": ".",
        "datasetId": ds_id, "version": version,
    })
    got = readback.get("examples", readback if isinstance(readback, list) else [])
    print(f"✓ dataset 版本 v{version} 已冻结，读回 {len(got)} 条")
    return version


# ── Step 4：自定义 evaluator ───────────────────────────────────────────────


def ensure_policy_grounding() -> str:
    """建（或复用）PolicyGrounding evaluator，返回它的 id。"""
    rubric = load_json("evaluators/policy_grounding.json")
    target_name = f"selfevolve_demo_{rubric['name']}"

    listed = omni("manage_evaluations", {"action": "list_evaluators", "project_path": "."})
    for ev in listed.get("evaluators", []):
        if ev.get("name") == target_name or ev.get("id") == target_name:
            print(f"✓ 复用已有 evaluator {target_name} ({ev.get('id')})")
            return ev["id"]

    created = omni("manage_evaluations", {
        "action": "create_evaluator", "project_path": ".",
        "name": target_name,
        "description": rubric["description"],
        "level": rubric["level"],
        "instructions": rubric["instructions"],
        "rating_scale": rubric["rating_scale"],
        "model_id": rubric["model_id"],
    })
    ev_id = created.get("evaluator_id") or created.get("evaluatorId") or created.get("id")
    print(f"✓ 新建 evaluator {target_name} ({ev_id})")
    return ev_id


def verify_evaluator_semantics(ev_ids: dict[str, str], declared: dict) -> dict:
    """尽力读取每个 evaluator 的真实 rating scale，与冻结声明核对。

    内置 evaluator 的值域文档没有明说（见 evaluators/semantics.json 的
    _verification_note）。这里读到就用真值、并报告差异；读不到就沿用声明值 ——
    但**无论哪种，实际使用的语义都会写进 run manifest**，事后可追溯。
    """
    resolved: dict[str, dict] = {}
    for logical, ev_id in ev_ids.items():
        sem = dict(declared[logical])
        sem["evaluator_id"] = ev_id
        sem["semantics_source"] = "declared"
        try:
            info = omni("manage_evaluations", {
                "action": "get_evaluator", "project_path": ".", "evaluator_id": ev_id,
            })
            scale = (info.get("rating_scale") or info.get("ratingScale") or {}).get("numerical")
            if scale:
                values = [float(s["value"]) for s in scale]
                actual_min, actual_max = min(values), max(values)
                if (actual_min, actual_max) != (float(sem["min"]), float(sem["max"])):
                    print(f"  ⚠️  {logical} 值域实测 [{actual_min},{actual_max}]，"
                          f"声明 [{sem['min']},{sem['max']}] —— 以实测为准，"
                          f"请同步更新 evaluators/semantics.json 并记入 BUILD-LOG")
                sem["min"], sem["max"] = actual_min, actual_max
                sem["semantics_source"] = "discovered"
        except OmniError:
            pass  # 内置 evaluator 通常不支持 get_evaluator，沿用声明值
        resolved[logical] = sem
    return resolved


# ── Step 5：invoke ────────────────────────────────────────────────────────


def invoke_all(examples: list[dict], run_dir: Path) -> list[dict]:
    """逐条调用本地 agent，并把 trace 找出来。"""
    results = []
    for i, ex in enumerate(examples, start=1):
        sid = ex["scenario_id"]
        session_id = f"baseline-{sid}-{int(time.time())}"
        t0 = time.time()
        try:
            resp = omni("manage_test_agent", {
                "action": "invoke_agent", "project_path": ".",
                "prompt": ex["turns"][0]["input"], "session_id": session_id,
            })
            rec = {
                "scenario_id": sid, "session_id": session_id, "status": "ok",
                "latency_ms": round((time.time() - t0) * 1000),
                "response": resp,
                "trace_id": resp.get("traceId") or resp.get("trace_id"),
            }
            print(f"  [{i:2d}/{len(examples)}] ✓ {sid}  {rec['latency_ms']:>6}ms  trace={rec['trace_id']}")
        except OmniError as e:
            rec = {"scenario_id": sid, "session_id": session_id, "status": "error",
                   "latency_ms": round((time.time() - t0) * 1000), "error": str(e), "trace_id": None}
            print(f"  [{i:2d}/{len(examples)}] ✗ {sid}  {e}")
        results.append(rec)

    # invoke 的返回不一定带 traceId；补一轮从本地 trace store 里按时间窗补齐。
    missing = [r for r in results if not r.get("trace_id")]
    if missing:
        print(f"  {len(missing)} 条没拿到 traceId，从本地 trace store 补齐…")
        time.sleep(5)  # 给 collector 落盘一点时间
        listed = omni("search_local_telemetry", {
            "operation": "list", "project_path": ".",
            # 注意单位：本地 store 的 window 是**毫秒**（云端 SQL 是秒）——
            # 见 docs/BUILD-LOG.md Phase -1.6 第 4 条
            "window": {"start": int(time.time() * 1000) - 3_600_000, "end": int(time.time() * 1000)},
            "limit": 500,
        })
        traces = listed.get("traces", [])
        for rec in missing:
            for t in traces:
                blob = json.dumps(t, ensure_ascii=False)
                if rec["session_id"] in blob:
                    rec["trace_id"] = t.get("traceId") or t.get("trace_id")
                    rec["trace_matched_by"] = "session_id"
                    break

    (run_dir / "invocations.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
    got = sum(1 for r in results if r.get("trace_id"))
    print(f"✓ invoke 完成：{got}/{len(results)} 条拿到 trace")
    return results


def attach_source_trace_ids(ds_id: str, examples: list[dict], invocations: list[dict]) -> None:
    """把 traceId 写回 dataset example 的 metadata.sourceTraceId。

    **这是避免位置匹配的关键一步**（skill Step 8.3）。写回之后，
    manage_evaluations 用 traceIds + datasetId 就能按 sourceTraceId 显式对齐。
    """
    by_sid = {r["scenario_id"]: r.get("trace_id") for r in invocations}
    updated = []
    for ex in examples:
        tid = by_sid.get(ex["scenario_id"])
        if not tid:
            continue
        e = json.loads(json.dumps(ex))  # 深拷贝，不改原 dataset 文件
        e["metadata"] = {**e.get("metadata", {}), "sourceTraceId": tid}
        updated.append(e)
    if not updated:
        print("  ⚠️  没有任何 example 能关联到 trace，跳过写回")
        return
    omni("manage_datasets", {
        "action": "update_examples", "dataSource": "local", "project_path": ".",
        "datasetId": ds_id, "examples": updated,
    })
    print(f"✓ 已把 sourceTraceId 写回 {len(updated)} 条 example")


# ── Step 6：评估 ──────────────────────────────────────────────────────────


def run_evaluators(ds_id: str, semantics: dict, invocations: list[dict], run_dir: Path) -> dict:
    """对所有 trace 跑每个 evaluator。"""
    trace_ids = [r["trace_id"] for r in invocations if r.get("trace_id")]
    if not trace_ids:
        raise SystemExit("没有可评估的 trace —— 先确认 invoke 与 collector 正常")

    all_results: dict[str, dict] = {}
    for logical, sem in semantics.items():
        ev_id, level = sem["evaluator_id"], sem["level"]
        print(f"  评估 {logical} (level={level}) …", end="", flush=True)
        t0 = time.time()
        try:
            res = omni("manage_evaluations", {
                "action": "run", "project_path": ".",
                "evaluatorId": ev_id,
                "traceIds": trace_ids,
                "dataSource": "local",
                # 提供 ground truth（expected_response / assertions / expected_trajectory）。
                # 匹配靠上一步写回的 sourceTraceId，不是位置。
                "datasetId": ds_id,
                "evaluatorLevel": level,
            }, timeout_s=1800)
            all_results[logical] = {"status": "ok", "elapsed_s": round(time.time() - t0, 1), "raw": res}
            print(f" 完成 {round(time.time()-t0,1)}s")
        except OmniError as e:
            all_results[logical] = {"status": "error", "elapsed_s": round(time.time() - t0, 1), "error": str(e)}
            print(f" 失败：{e}")

    (run_dir / "evaluation-raw.json").write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    return all_results


def extract_scores(raw: dict, semantics: dict) -> dict[str, dict[str, float | None]]:
    """从各 evaluator 的原始返回里抽出 per-scenario 的分数。

    返回不同 evaluator 的形状可能不同，所以这里做宽松解析，
    并且**把解析不出来的记为 None（unscored），绝不当成 0**——
    skill Step 8.8 / 失败处理都强调：evaluator 缺失只降低 coverage，
    不能重新解释为 0 分，否则会凭空造出"candidate 变差了"的结论。
    """
    out: dict[str, dict[str, float | None]] = {}
    for logical, block in raw.items():
        scores: dict[str, float | None] = {}
        if block.get("status") != "ok":
            out[logical] = scores
            continue
        rows = block["raw"].get("results") or block["raw"].get("evaluations") or []
        for row in rows if isinstance(rows, list) else []:
            key = (row.get("key") or row.get("targetKey") or row.get("traceId")
                   or row.get("scenario_id") or "")
            score = row.get("score", row.get("value"))
            if score is None or score == -1:  # -1 是 Omni 的 "unscored" 约定
                scores[key] = None
                continue
            try:
                scores[key] = canonical(float(score), semantics[logical])
            except (TypeError, ValueError):
                scores[key] = None
        out[logical] = scores
    return out


# ── Step 7：确定性门 ──────────────────────────────────────────────────────

AMOUNT_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*元")


def deterministic_gates(examples: list[dict], invocations: list[dict]) -> list[dict]:
    """跑 semantics.json 里声明的两个确定性检查。

    这些不经过 LLM judge，所以结论无争议；skill Step 8.8 规定
    critical deterministic failure 覆盖 judge 分数。
    """
    by_sid = {r["scenario_id"]: r for r in invocations}
    findings = []
    for ex in examples:
        sid = ex["scenario_id"]
        inv = by_sid.get(sid) or {}
        text = json.dumps(inv.get("response", ""), ensure_ascii=False)

        # 门 2：不可退的订单不得报出退款金额
        must_not_quote = any("不得承诺任何退款金额" in a for a in ex["assertions"])
        if must_not_quote:
            quoted = AMOUNT_RE.findall(text)
            # 排除政策数值本身（如"14 天"不带元）；只看带"元"的金额
            findings.append({
                "scenario_id": sid, "gate": "no_amount_when_ineligible",
                "critical": True,
                "passed": not quoted,
                "detail": f"回答中出现金额 {quoted[:5]}" if quoted else "未报出退款金额",
            })
    return findings


# ── Step 8：报告 ──────────────────────────────────────────────────────────


def write_report(run_dir: Path, manifest: dict, examples: list[dict],
                 invocations: list[dict], scores: dict, gates: list[dict]) -> Path:
    lines: list[str] = []
    a = lines.append

    a("# Phase 2 — 本地基线报告\n")
    a(f"- run_id：`{manifest['run_id']}`")
    a(f"- 生成时间：{manifest['finished_at']}")
    a(f"- prompt 版本：`{manifest['prompt_version']}`（hash `{manifest['prompt_hash'][:16]}`）")
    a(f"- 模型：`{manifest['model_id']}`")
    a(f"- 启用工具：{', '.join(manifest['tools_enabled']) or '（未获取到）'}")
    a(f"- dataset：`{manifest['dataset_name']}` v{manifest['dataset_version']}"
      f"（{len(examples)} 条，id `{manifest['dataset_id']}`）")
    a(f"- 基准日 `DEMO_AS_OF_DATE`：{manifest['as_of_date']}\n")

    ok = sum(1 for r in invocations if r["status"] == "ok")
    traced = sum(1 for r in invocations if r.get("trace_id"))
    a("## 执行情况\n")
    a(f"| 指标 | 值 |\n|---|---|")
    a(f"| invoke 成功 | {ok} / {len(invocations)} |")
    a(f"| 拿到 trace | {traced} / {len(invocations)} |")
    lat = [r["latency_ms"] for r in invocations if r["status"] == "ok"]
    if lat:
        lat_sorted = sorted(lat)
        a(f"| 延迟 中位数 / p95 | {lat_sorted[len(lat_sorted)//2]} ms / "
          f"{lat_sorted[min(len(lat_sorted)-1, int(len(lat_sorted)*0.95))]} ms |")
    a("")

    a("## Evaluator 分数（已按 semantics.json 归一化到 0–1，越高越好）\n")
    a("| Evaluator | 层级 | 已评分 | 均值 | 及格率 | 冻结值域 |")
    a("|---|---|---:|---:|---:|---|")
    for logical, sem in manifest["evaluator_semantics"].items():
        s = scores.get(logical, {})
        vals = [v for v in s.values() if v is not None]
        pass_canon = canonical(float(sem["pass_threshold"]), sem)
        n_pass = sum(1 for v in vals if v >= pass_canon)
        mean = f"{sum(vals)/len(vals):.3f}" if vals else "—"
        rate = f"{100*n_pass/len(vals):.0f}%" if vals else "—"
        a(f"| `{logical}` | {sem['level']} | {len(vals)}/{len(s) or len(invocations)} | "
          f"{mean} | {rate} | [{sem['min']},{sem['max']}] {sem['direction']} |")
    a("")
    a("> **未评分（unscored）一律记 `None`，绝不当 0 分。** evaluator 缺失只降低 coverage —— "
      "把缺失当 0 会凭空造出「变差了」的结论（skill Step 8.8）。\n")

    a("## 确定性门\n")
    if gates:
        failed = [g for g in gates if not g["passed"]]
        a(f"- `no_amount_when_ineligible`：{len(gates)-len(failed)}/{len(gates)} 通过"
          f"{'（critical）' if failed else ''}")
        if failed:
            a("\n违反明细（对不可退订单报出了退款金额 —— 会造成真实客诉，比算错金额更严重）：\n")
            a("| scenario | 详情 |\n|---|---|")
            for g in failed:
                a(f"| `{g['scenario_id']}` | {g['detail']} |")
    else:
        a("（本轮没有需要该门检查的场景）")
    a("")

    a("## 按原型看失败分布\n")
    a("| 原型 | scenario | PolicyGrounding | Correctness | 备注 |")
    a("|---|---|---:|---:|---|")
    pg, corr = scores.get("PolicyGrounding", {}), scores.get("Builtin.Correctness", {})
    by_sid = {r["scenario_id"]: r for r in invocations}
    for ex in examples:
        sid = ex["scenario_id"]
        tid = by_sid.get(sid, {}).get("trace_id") or ""
        f = lambda d: next((f"{v:.2f}" for k, v in d.items() if sid in str(k) or (tid and tid in str(k))  # noqa: E731
                            ) if v is not None else "—", "—")
        note = "invoke 失败" if by_sid.get(sid, {}).get("status") != "ok" else ""
        a(f"| {ex['metadata']['archetype']} | `{sid}` | {f(pg)} | {f(corr)} | {note} |")
    a("")

    a("## 产物\n")
    a(f"- `{run_dir.relative_to(REPO)}/manifest.json` —— 冻结的实验配置（evaluator 语义、dataset 版本、prompt hash）")
    a(f"- `{run_dir.relative_to(REPO)}/invocations.jsonl` —— 每条调用的输入/输出/trace id")
    a(f"- `{run_dir.relative_to(REPO)}/evaluation-raw.json` —— evaluator 原始返回")
    a("")
    a("## 下一步\n")
    a("Phase 3：`scripts/build_agent_bundle.sh` → `cd cdk && npm run deploy` → "
      "`python scripts/traffic.py --dataset prod_sim`。")

    report = REPO / "reports" / "01-local-baseline.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


# ── main ──────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-invoke", action="store_true", help="复用上次 run 的 trace，只重跑评估")
    ap.add_argument("--run-dir", default="", help="配合 --skip-invoke 指定要复用的 run 目录")
    args = ap.parse_args()

    require_omni()

    dataset = load_json(f"datasets/{DATASET_NAME}.json")
    examples = dataset["examples"][: args.limit] if args.limit else dataset["examples"]
    sem_file = load_json("evaluators/semantics.json")

    started = datetime.now(timezone.utc)
    run_dir = (REPO / args.run_dir) if args.run_dir else (
        REPO / "runs" / f"baseline-{started.strftime('%Y%m%dT%H%M%SZ')}")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run 目录：{run_dir.relative_to(REPO)}\n")

    print("[1/7] 配置本地 agent")
    configure_agent()

    print("\n[2/7] 启动 collector 与 dev server")
    ensure_server()

    print("\n[3/7] 注册并冻结 dataset")
    ds_id = ensure_dataset(examples, dataset["description"])

    print("\n[4/7] 准备 evaluator")
    pg_id = ensure_policy_grounding()
    ev_ids = {name: (pg_id if name == "PolicyGrounding" else name)
              for name in sem_file["quality_evaluators"] if not name.startswith("_")}
    semantics = verify_evaluator_semantics(
        ev_ids, {k: v for k, v in sem_file["quality_evaluators"].items() if not k.startswith("_")})
    print(f"✓ 冻结 {len(semantics)} 个质量 evaluator 的打分语义")

    print("\n[5/7] 调用 agent")
    if args.skip_invoke:
        invocations = [json.loads(l) for l in (run_dir / "invocations.jsonl").read_text().splitlines() if l.strip()]
        print(f"✓ 复用 {len(invocations)} 条已有调用记录")
    else:
        invocations = invoke_all(examples, run_dir)
        attach_source_trace_ids(ds_id, examples, invocations)

    ds_version = freeze_dataset_version(ds_id)

    print("\n[6/7] 运行 evaluator")
    raw = run_evaluators(ds_id, semantics, invocations, run_dir)
    scores = extract_scores(raw, semantics)
    gates = deterministic_gates(examples, invocations)

    print("\n[7/7] 写报告")
    first_ok = next((r for r in invocations if r["status"] == "ok"), {})
    echoed = first_ok.get("response", {}) if isinstance(first_ok.get("response"), dict) else {}
    manifest = {
        "run_id": run_dir.name,
        "phase": "2-local-baseline",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "dataset_name": DATASET_NAME, "dataset_id": ds_id, "dataset_version": ds_version,
        "prompt_name": PROMPT_NAME,
        "prompt_version": echoed.get("prompt_version", "（未从响应中获取到）"),
        "prompt_hash": echoed.get("prompt_hash", ""),
        "model_id": echoed.get("model_id", "（未从响应中获取到）"),
        "tools_enabled": echoed.get("tools_enabled", []),
        "as_of_date": "2026-09-17",
        "evaluator_semantics": semantics,
        "gates": sem_file["gates"],
        "start_command": START_COMMAND,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report = write_report(run_dir, manifest, examples, invocations, scores, gates)
    print(f"\n✓ 报告：{report.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
