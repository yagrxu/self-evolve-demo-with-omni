#!/usr/bin/env python3
"""SKILL.md Step 8 —— 在 control 集上做 paired replay。

为什么必须是脚本而不是让 agent 自己发调用
------------------------------------------
SKILL.md Step 8 的约束原文（大意）：这个循环是 variant × example × repetition 的
**确定性记账**，手工驱动会漏格、重格。而漏掉的格子不会报错，只会让某个 arm 的均值
悄悄偏移 —— 这正是最难发现的一类污染。

三条硬约束（都在代码里落实，不靠自觉）
--------------------------------------
1. **baseline 必须在本步现场重跑**，不复用 Phase 2/3 的历史分数。
   那些分数来自不同流量、不同时刻，不构成配对，Step 9 的 paired bootstrap 会失效。
2. **变体只能靠 ``OMNI_PROMPTS_OVERRIDE`` 切换**，绝不编辑共享的 prompts.json。
   一次漏改回来就会把整个 arm 静默错标。
3. **每次调用都要核对实际生效的 prompt 版本**，不匹配就丢弃重跑。
   一条错标就能带偏那个 arm 的均值。

用法：
    python scripts/replay.py --run prod_sim-0be7e8bc+dev_local-30130a4e
    python scripts/replay.py --run <run_id> --set control --repetitions 3
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from omni_client import OmniError, omni, require_omni  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROMPT_NAME = "order_support"
REGION = "us-west-2"          # 见 docs/BUILD-LOG.md 决策点 R4' / 5.4
BASELINE_ID = "baseline"
DEV_PORT = 8080


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def prompts_path_for(variant: str, run_dir: Path) -> Path:
    """变体 id → 那份独立的 prompts.json。baseline 用仓库里的原件。"""
    if variant == BASELINE_ID:
        return REPO / "agent" / "prompts.json"
    return REPO / ".omni" / "self-evolution" / "candidates" / variant / "prompts.json"


def expected_version(variant: str, run_dir: Path) -> str:
    """用 agent 自己的 prompt_loader 解析版本号，**不要自己猜**。

    实测踩过（BUILD-LOG 5.1）：第一版直接读顶层 `version` 键，而 loader 根本不看它 ——
    它从 `history[-1].versionId` 推导，并在 `messages` 与该条不一致时追加 `-draft`。
    于是候选 arm 的 trace 上是 `order_support-v1-draft`，与我期望的 id 不符，
    Step 8 的版本核对把**整个 arm 全部丢弃**。

    核对通过了才有意义，所以这里必须和运行时用同一个函数 ——
    任何"平行实现"都会在 loader 改动时静默失配。
    """
    import importlib
    import prompt_loader

    override = prompts_path_for(variant, run_dir).resolve()
    prev = os.environ.get("OMNI_PROMPTS_OVERRIDE")
    try:
        if variant == BASELINE_ID:
            os.environ.pop("OMNI_PROMPTS_OVERRIDE", None)
        else:
            os.environ["OMNI_PROMPTS_OVERRIDE"] = str(override)
        importlib.reload(prompt_loader)
        return prompt_loader.get_prompt_version(PROMPT_NAME)
    finally:
        if prev is None:
            os.environ.pop("OMNI_PROMPTS_OVERRIDE", None)
        else:
            os.environ["OMNI_PROMPTS_OVERRIDE"] = prev


def start_server(override: Path | None) -> None:
    """用指定的 prompts 覆盖重启本地 dev server。

    必须重启：``OMNI_PROMPTS_OVERRIDE`` 是进程级环境变量，
    prompt_loader 在进程内会缓存 —— 不重启就还是上一个变体，
    而响应看起来完全正常。
    """
    try:
        omni("local_server", {"action": "stop"})
    except OmniError:
        pass
    time.sleep(2)

    # **必须显式钉住 region。** 实测（BUILD-LOG 5.4）：Omni 的 Local dev 把
    # AWS_REGION 设成 us-east-1，于是 agent 去打 bedrock-runtime.us-east-1，
    # 而那个 endpoint 在本机 DNS 解析到 100.64/10（CGNAT，疑似 VPN 注入路由），
    # 连接间歇性失败 —— 候选 arm 因此整片拿不到分。
    # 决策点 R4' 要求全链路 us-west-2，本地 replay 也不例外。
    env_line = f"OMNI_PROMPTS_OVERRIDE={override} " if override else ""
    cmd = (f"cd agent && AWS_REGION={REGION} AWS_DEFAULT_REGION={REGION} {env_line}"
           f"../.venv/bin/opentelemetry-instrument ../.venv/bin/python agent.py")
    omni("manage_test_agent", {"action": "set_start_command", "project_path": ".",
                               "command": cmd, "port": DEV_PORT})
    omni("local_server", {"action": "start"})
    for _ in range(24):  # 最多等 120s
        time.sleep(5)
        if omni("manage_test_agent", {"action": "ping", "project_path": "."}).get("active"):
            return
    out = omni("local_server", {"action": "get_output", "lines": 40})
    raise SystemExit(f"dev server 起不来（override={override}）：\n"
                     f"{json.dumps(out, ensure_ascii=False, indent=2)}")


def trace_for_session(session_id: str, since_ms: int,
                      attempts: int = 4, gap_s: float = 3.0
                      ) -> tuple[str | None, str | None, int | None]:
    """按 span 属性 session.id 找 trace，返回 (trace_id, prompt_version)。

    与 local_baseline.py 同一套做法：`list` 只给摘要（里面没有 session），
    必须再 `get` 才能拿到 spans。见 BUILD-LOG 2.1.4。

    ⚠️ 窗口必须**紧贴这次调用**，不能用宽窗口（实测踩过，BUILD-LOG 5.2）：
    本地 trace store 会跨 run 累积（跑到一半时已有 281 条），而 `list` 是分页的
    （默认 50 条、`hasMore: true`）。用 600 秒宽窗口 + limit 200 去捞时，
    刚产生的那条**未必在返回页里** —— 于是 trace 找不到、格子被判为
    `discarded_no_version`，baseline arm 从 75/75 掉到 23/75。
    窄窗口让这次查找的代价与 store 总量无关。

    另外要重试：collector 落盘有延迟，第一次查不到不代表没有。
    """
    for attempt in range(attempts):
        if attempt:
            time.sleep(gap_s)
        listed = omni("search_local_telemetry", {
            "operation": "list", "project_path": ".",
            "window": {"start": since_ms - 5_000, "end": int(time.time() * 1000) + 5_000},
            "limit": 50})
        ids = [t.get("id") for t in listed.get("traces", []) if t.get("id")]
        if not ids:
            continue
        found = _match_session(ids, session_id)
        if found[0]:
            return found
    return None, None, None


def _match_session(ids: list[str], session_id: str) -> tuple[str | None, str | None, int | None]:
    details = omni("search_local_telemetry", {
        "operation": "get", "project_path": ".", "traceIds": ids})
    details = details if isinstance(details, list) else (details.get("traces") or [details])
    for t in details:
        blob = json.dumps(t, ensure_ascii=False)
        if session_id not in blob:
            continue
        ver = None

        def walk(n):
            nonlocal ver
            if isinstance(n, dict):
                for k, v in n.items():
                    if k == "llm.prompt_template.version" and isinstance(v, str):
                        ver = ver or v
                    else:
                        walk(v)
            elif isinstance(n, list):
                for i in n:
                    walk(i)

        walk(t)
        # token 用量从 **trace** 取，不从响应信封取 —— `invoke_agent` 的信封里没有它
        # （实测：门禁因此判「未观测到 token」而按「缺失即失败」拒掉候选）。
        usage = t.get("tokenUsage") or {}
        total = next((usage[k] for k in ("totalTokens", "total_tokens", "total")
                      if isinstance(usage.get(k), (int, float))), None)
        return t.get("id"), ver, total
    return None, None, None


def replay_variant(variant: str, examples: list[dict], reps: int, run_dir: Path) -> list[dict]:
    override = None if variant == BASELINE_ID else prompts_path_for(variant, run_dir)
    want = expected_version(variant, run_dir)
    print(f"\n── arm `{variant}`（期望 prompt 版本 `{want}`）")
    start_server(override)

    out_dir = run_dir / "runs" / variant
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    for rep in range(1, reps + 1):
        for i, ex in enumerate(examples, start=1):
            sid_scenario = ex["scenario_id"]
            session_id = f"replay-{variant}-{sid_scenario}-r{rep}-{int(time.time()*1000)}"
            t0 = time.time()
            rec: dict = {"variant": variant, "scenario_id": sid_scenario,
                         "repetition": rep, "session_id": session_id}
            try:
                resp = omni("manage_test_agent", {
                    "action": "invoke_agent", "project_path": ".",
                    "prompt": ex["turns"][0]["input"], "session_id": session_id})
                # ⚠️ `invoke_agent` 会以 **HTTP 成功 + 错误信封**的形式返回失败：
                #   {"error": "connection_failed", "message": "…Server returned 500…",
                #    "sessionId": "…"}
                # `omni()` 抓不到它（三个键，且 MCP 调用本身是成功的）。
                # 实测（BUILD-LOG 5.4）第一版把这种格子记成 ok，于是 75/75「有效」，
                # 而 evaluator 全给 -1（"Span has an error"）—— 门禁最终得到 0 个配对样本。
                # **有 error 键或输出为空，一律不算有效格。**
                text = resp.get("content") or ""
                if resp.get("error") or not text.strip():
                    rec.update({"status": "error",
                                "error": resp.get("message") or resp.get("error") or "空输出",
                                "latency_ms": round((time.time() - t0) * 1000),
                                "response": resp})
                    print(f"  r{rep} [{i:2d}/{len(examples)}] ✗ {sid_scenario} "
                          f"{str(rec['error'])[:110]}")
                    records.append(rec)
                    continue
                rec.update({"status": "ok",
                            "latency_ms": round((time.time() - t0) * 1000),
                            "output": text,
                            "response": resp})
            except OmniError as e:
                rec.update({"status": "error", "error": str(e),
                            "latency_ms": round((time.time() - t0) * 1000)})
                print(f"  r{rep} [{i:2d}/{len(examples)}] ✗ {sid_scenario}: {e}")
                records.append(rec)
                continue

            time.sleep(2)  # 给 collector 落盘
            tid, got_ver, total_tokens = trace_for_session(session_id, int(t0 * 1000))
            rec["trace_id"] = tid
            rec["prompt_version_on_trace"] = got_ver
            rec["total_tokens"] = total_tokens

            # 变体身份核对 —— 不匹配就丢弃这一格。
            # 宁可少一个观测，也不能让一格错标的数据进入均值。
            if got_ver and want and got_ver != want:
                rec["status"] = "discarded_version_mismatch"
                print(f"  r{rep} [{i:2d}/{len(examples)}] ⚠️ {sid_scenario} "
                      f"版本不符：trace 上是 `{got_ver}`，期望 `{want}` —— 丢弃")
            elif not got_ver:
                rec["status"] = "discarded_no_version"
                print(f"  r{rep} [{i:2d}/{len(examples)}] ⚠️ {sid_scenario} "
                      f"trace 上没有 prompt 版本 —— 丢弃（无法归因）")
            else:
                print(f"  r{rep} [{i:2d}/{len(examples)}] ✓ {sid_scenario} "
                      f"{rec['latency_ms']:>6}ms")
            records.append(rec)

    (out_dir / "invocations.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    ok = sum(1 for r in records if r["status"] == "ok")
    print(f"  arm `{variant}`：{ok}/{len(records)} 格有效")
    return records


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help=".omni/self-evolution/ 下的 run_id")
    ap.add_argument("--set", default="control", choices=["control", "holdout"])
    ap.add_argument("--repetitions", type=int, default=3)
    ap.add_argument("--variants", default="", help="逗号分隔；默认 baseline + 全部候选")
    args = ap.parse_args()

    require_omni()
    run_dir = REPO / ".omni" / "self-evolution" / args.run
    ds = load_json(run_dir / "datasets" / f"{args.set}.json")
    examples = ds["examples"]
    if not examples:
        raise SystemExit(f"{args.set}.json 是空的")

    cand_dir = REPO / ".omni" / "self-evolution" / "candidates"
    variants = ([v.strip() for v in args.variants.split(",") if v.strip()]
                or [BASELINE_ID] + sorted(p.name for p in cand_dir.iterdir() if p.is_dir()))
    if BASELINE_ID not in variants:
        raise SystemExit("baseline 必须参与本步 —— 复用历史分数会让 paired bootstrap 失效"
                         "（SKILL.md Step 8）")

    if args.repetitions < 3:
        raise SystemExit(f"重复次数 {args.repetitions} < 3。LLM 采样是不确定的，"
                         f"少于 3 遍拿不到样本内方差估计，分不清真实提升和重采样"
                         f"（SKILL.md Step 8）。")

    print(f"run={args.run}  集合={args.set}（{len(examples)} 条）  "
          f"变体={variants}  重复={args.repetitions}")
    print(f"总格数 = {len(variants)} × {len(examples)} × {args.repetitions} "
          f"= {len(variants)*len(examples)*args.repetitions}")

    started = datetime.now(timezone.utc)
    all_records: dict[str, list[dict]] = {}
    for v in variants:
        all_records[v] = replay_variant(v, examples, args.repetitions, run_dir)

    meta = {
        "run_id": args.run, "set": args.set, "variants": variants,
        "repetitions": args.repetitions, "example_count": len(examples),
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "expected_versions": {v: expected_version(v, run_dir) for v in variants},
        "valid_cells": {v: sum(1 for r in rs if r["status"] == "ok")
                        for v, rs in all_records.items()},
    }
    (run_dir / f"replay-{args.set}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n有效格数：{meta['valid_cells']}")
    print(f"✓ 元数据：.omni/self-evolution/{args.run}/replay-{args.set}.json")
    print(f"下一步：python scripts/gates.py --run {args.run} --set {args.set}")

    # 恢复 baseline 启动命令，避免把 override 留在 Omni 配置里污染后续操作
    start_server(None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
