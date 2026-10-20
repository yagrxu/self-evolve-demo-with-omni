#!/usr/bin/env python3
"""Omni 流程里原来是「临时命令」的几步，整理成可复跑的子命令。

  python3 training/omni/scripts/omni_steps.py preflight
  python3 training/omni/scripts/omni_steps.py snapshot   --agent "Strands Agents" --complaint "…"
  python3 training/omni/scripts/omni_steps.py ruler      --run <run_id>
  python3 training/omni/scripts/omni_steps.py candidate  --run <run_id> --id c1 --prompt-file c1.txt --target "…" --mechanism "…"
  python3 training/omni/scripts/omni_steps.py decide     --run <run_id> --winner c1 --gates gates-fresh.json [--writeback]

全部通过 scripts/omni_client.py 调 Omni MCP（Kiro 里的 CloudWatch Omni 扩展必须为本目录运行）。
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "agent"))
from omni_client import omni  # noqa: E402

PROMPT = "order_support"
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()  # noqa: E731
run_dir = lambda r: REPO / ".omni" / "self-evolution" / r  # noqa: E731


def loader(override: str | None = None):
    if override:
        os.environ["OMNI_PROMPTS_OVERRIDE"] = str(Path(override).resolve())
    else:
        os.environ.pop("OMNI_PROMPTS_OVERRIDE", None)
    import prompt_loader
    return importlib.reload(prompt_loader)


# ── Step 1 ──────────────────────────────────────────────────────────────
def preflight(a) -> None:
    c = omni("check_credentials", {})
    print(f"check_credentials: can_sign={c.get('can_sign')} ready={c.get('ready')} auth={c.get('auth_status')}")
    if not c.get("can_sign"):
        sys.exit("✗ 凭证不可签名")
    pl = loader()
    print(f"prompts.json: {PROMPT} 版本 {pl.get_prompt_version(PROMPT)} hash {pl.get_prompt_hash(PROMPT)}")
    print(f"model 来自 prompts.json: {pl.get_model_config(PROMPT)['modelId']}")
    if not omni("manage_test_agent", {"action": "ping", "project_path": "."}).get("active"):
        omni("manage_local_collector", {"action": "start", "project_path": "."})
        omni("local_server", {"action": "start"})
        for _ in range(24):
            time.sleep(5)
            if omni("manage_test_agent", {"action": "ping", "project_path": "."}).get("active"):
                break
        else:
            sys.exit("✗ dev server 起不来")
    print("ping: active")
    sid = f"preflight-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    t0 = int(time.time() * 1000)
    r = omni("manage_test_agent", {"action": "invoke_agent", "project_path": ".",
                                   "prompt": "订单号 ORD-10008。落地灯的灯罩我不喜欢，想退货", "session_id": sid})
    if r.get("error") or not (r.get("content") or "").strip():
        sys.exit(f"✗ invoke 失败：{r}")
    time.sleep(6)
    lst = omni("search_local_telemetry", {"operation": "list", "project_path": ".",
                                          "window": {"start": t0 - 3000, "end": int(time.time() * 1000) + 5000}, "limit": 10})
    ids = [t["id"] for t in lst.get("traces", [])]
    traces = omni("search_local_telemetry", {"operation": "get", "project_path": ".", "traceIds": ids})
    t = next((x for x in traces if sid in json.dumps(x, ensure_ascii=False)), None)
    if not t:
        sys.exit("✗ 找不到这次调用的 trace")
    md = [s.get("metadata", {}) for s in t["spans"]]
    v = sum(1 for m in md if m.get("llm.prompt_template.version"))
    h = sum(1 for m in md if m.get("llm.prompt_template.hash"))
    print(f"trace {t['id']}：{len(md)} 个 span，带 version {v}，带 hash {h}，tokens {t.get('tokenUsage', {}).get('total')}")
    if not v or not h:
        sys.exit("✗ span 上缺 prompt 版本 / hash —— 检查是否用 opentelemetry-instrument 启动")
    print("✓ preflight 通过")


# ── Step 2 ──────────────────────────────────────────────────────────────
def snapshot(a) -> None:
    pl = loader()
    now = datetime.now(timezone.utc)
    rid = a.agent.lower().replace(" ", "_") + "-" + now.strftime("%Y%m%dT%H%M%SZ")
    rd = run_dir(rid)
    if subprocess.run(["git", "check-ignore", "-q", str(rd)], cwd=REPO).returncode != 0:
        sys.exit("✗ .omni/ 没有被 git 忽略 —— trace 可能含客户数据")
    rd.mkdir(parents=True)
    tree = ast.parse((REPO / "agent" / "agent.py").read_text())
    docs = {n.name: ast.get_docstring(n) or "" for n in tree.body if isinstance(n, ast.FunctionDef)}
    tools = ["get_order", "get_refund_policy", "search_knowledge_base", "create_return_label"]
    pol = json.loads((REPO / "agent/fixtures/policies.json").read_text())
    p = pl._load()[PROMPT]
    m = {"run_id": rid, "agent_name": a.agent, "quality_complaint": a.complaint,
         "started_at": now.isoformat(),
         "_doc": "全层状态快照；不在这里决定改哪一层",
         "layers": {
             "policy": {"sha256": sha(REPO / "agent/fixtures/policies.json"), "version": pol.get("_version")},
             "data_fixtures": {"orders.json": sha(REPO / "agent/fixtures/orders.json")},
             "tool": {"tier": os.environ.get("AGENT_TOOL_TIER", "basic"), "active": sorted(tools),
                      "description_sha256": {t: hashlib.sha256(docs.get(t, "").encode()).hexdigest()[:16] for t in tools},
                      "implementation_sha256": sha(REPO / "agent/tools.py")},
             "workflow": {"sha256": sha(REPO / "agent/agent.py")},
             "skill": {"loaded": []},
             "prompt": {"version": pl.get_prompt_version(PROMPT), "hash": pl.get_prompt_hash(PROMPT),
                        "prompts_file_sha256": sha(REPO / "agent/prompts.json"),
                        "system_prompt": p["messages"][0]["content"]},
             "model": p["model"]},
         "code": {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
                  "uncommitted": subprocess.check_output(["git", "status", "--porcelain", "agent/"], cwd=REPO).decode().splitlines()},
         "datasets_sha256": {d.stem: sha(d) for d in sorted((REPO / "datasets").glob("*.json"))}}
    (rd / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    print(rid)
    for k, v in m["layers"].items():
        print(f"  {k:14s} {json.dumps(v, ensure_ascii=False)[:120]}")


# ── Step 4（replay 用的标尺）────────────────────────────────────────────
def ruler(a) -> None:
    rd = run_dir(a.run)
    tpl = json.loads((REPO / "training/omni/evaluators.template.json").read_text())
    lst = omni("manage_evaluations", {"action": "list_evaluators", "project_path": "."})["evaluators"]
    pg = next((e["id"] for e in lst if e.get("name") == "selfevolve_demo_PolicyGrounding"), None)
    for name, e in tpl["evaluators"].items():
        if name == "PolicyGrounding":
            if not pg:
                sys.exit("✗ 找不到自定义 evaluator selfevolve_demo_PolicyGrounding")
            e["id"] = pg
        g = omni("manage_evaluations", {"action": "get_evaluator", "evaluator_id": e["id"]})
        scale = [x["value"] for x in ((g.get("ratingScale") or {}).get("numerical") or [])]
        if scale and (min(scale), max(scale)) != (e["min"], e["max"]):
            sys.exit(f"✗ {name} 声明值域 [{e['min']},{e['max']}] 与 get_evaluator 实测 {scale} 不一致")
        print(f"  {name:32s} {e['role']:7s} [{e['min']},{e['max']}] 及格 {e['pass_threshold']}  ✓ 与 get_evaluator 一致")
    tpl["frozen_at"] = datetime.now(timezone.utc).isoformat(); tpl["run_id"] = a.run
    p = rd / "evaluators.json"
    p.write_text(json.dumps(tpl, ensure_ascii=False, indent=2)); p.chmod(0o444)
    m = json.loads((rd / "manifest.json").read_text()); m["evaluators_sha256"] = sha(p)
    (rd / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    print(f"✓ {p.relative_to(REPO)} 已冻结（只读），sha256 {m['evaluators_sha256'][:12]}…")


# ── Step 7 ──────────────────────────────────────────────────────────────
def candidate(a) -> None:
    rd = run_dir(a.run)
    base = json.loads((REPO / "agent/prompts.json").read_text())
    text = Path(a.prompt_file).read_text().strip()
    for bad in a.forbid.split(","):
        if bad and bad in text:
            sys.exit(f"✗ prompt 里出现了禁止内容 {bad!r}（订单号 / 标准答案 / 越层的工具名）")
    d = copy.deepcopy(base); p = d[PROMPT]; p["messages"][0]["content"] = text
    p["history"].append({"version": len(p["history"]) + 1, "versionId": a.id, "timestamp": int(time.time() * 1000),
                         "commitMessage": f"candidate {a.id} ({a.run})", "messages": copy.deepcopy(p["messages"]),
                         "model": copy.deepcopy(p["model"]), "labels": ["candidate"]})
    chk = copy.deepcopy(d); chk[PROMPT]["messages"][0]["content"] = base[PROMPT]["messages"][0]["content"]
    chk[PROMPT]["history"] = chk[PROMPT]["history"][:-1]
    assert json.dumps(chk, sort_keys=True) == json.dumps(base, sort_keys=True), "除 prompt 外有差异"
    cd = rd / "candidates" / a.id; cd.mkdir(parents=True, exist_ok=True)
    (cd / "prompts.json").write_text(json.dumps(d, ensure_ascii=False, indent=2))
    pl = loader(cd / "prompts.json")
    assert pl.get_prompt_version(PROMPT) == a.id
    (cd / "rationale.md").write_text(f"# {a.id}\n\n**针对**：{a.target}\n\n**机制**：{a.mechanism}\n\n"
                                     f"- 版本 `{a.id}` · hash `{pl.get_prompt_hash(PROMPT)}` · {len(text)} 字\n\n```\n{text}\n```\n")
    print(f"✓ 候选 {a.id}：{len(text)} 字，hash {pl.get_prompt_hash(PROMPT)}；除 system prompt 外与 baseline 逐键一致")


# ── Step 11 ─────────────────────────────────────────────────────────────
def decide(a) -> None:
    rd = run_dir(a.run)
    m = json.loads((rd / "manifest.json").read_text())
    g = json.loads((rd / a.gates).read_text())
    pl = loader()
    tamper_ok = (pl.get_prompt_hash(PROMPT) == m["layers"]["prompt"]["hash"]
                 and sha(REPO / "agent/prompts.json") == m["layers"]["prompt"]["prompts_file_sha256"])
    layers_ok = {"policy": sha(REPO / "agent/fixtures/policies.json") == m["layers"]["policy"]["sha256"],
                 "tools.py": sha(REPO / "agent/tools.py") == m["layers"]["tool"]["implementation_sha256"],
                 "agent.py": sha(REPO / "agent/agent.py") == m["layers"]["workflow"]["sha256"]}
    res = next((r for r in g["results"] if r["variant"] == a.winner), None)
    passed = bool(res and res.get("passed"))
    outcome = ("NO_DECISION" if not tamper_ok or not all(layers_ok.values())
               else "WINNER" if passed else "NO_CHANGE")
    dec = {"run_id": a.run, "outcome": outcome, "candidate": a.winner, "gates_file": a.gates,
           "tamper_check": tamper_ok, "other_layers_unchanged": layers_ok,
           "baseline": m["layers"]["prompt"], "result": res, "deployed": False,
           "decided_at": datetime.now(timezone.utc).isoformat()}
    if outcome == "WINNER" and a.writeback:
        wb = rd / "writeback"; wb.mkdir(exist_ok=True)
        shutil.copy(REPO / "agent/prompts.json", wb / "prompts.before.json")
        shutil.copy(rd / "candidates" / a.winner / "prompts.json", REPO / "agent/prompts.json")
        (wb / "REVERT.sh").write_text(f"#!/bin/bash\ncp '{wb / 'prompts.before.json'}' '{REPO / 'agent/prompts.json'}'\n")
        dec["writeback"] = {"file": "agent/prompts.json", "revert": f"bash {wb / 'REVERT.sh'}"}
    (rd / "decision.json").write_text(json.dumps(dec, ensure_ascii=False, indent=2))
    print(json.dumps({k: dec[k] for k in ("outcome", "candidate", "tamper_check", "other_layers_unchanged")},
                     ensure_ascii=False))
    if "writeback" in dec:
        print(f"✓ 已写回本地 agent/prompts.json（未部署）。回滚：{dec['writeback']['revert']}")


def main() -> None:
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    s = sub.add_parser("snapshot"); s.add_argument("--agent", required=True); s.add_argument("--complaint", required=True)
    r = sub.add_parser("ruler"); r.add_argument("--run", required=True)
    c = sub.add_parser("candidate"); c.add_argument("--run", required=True); c.add_argument("--id", required=True)
    c.add_argument("--prompt-file", required=True); c.add_argument("--target", required=True)
    c.add_argument("--mechanism", required=True)
    c.add_argument("--forbid", default="ORD-,create_return_label,calculate_refund,get_coupon_allocation")
    d = sub.add_parser("decide"); d.add_argument("--run", required=True); d.add_argument("--winner", required=True)
    d.add_argument("--gates", required=True); d.add_argument("--writeback", action="store_true")
    a = ap.parse_args()
    {"preflight": preflight, "snapshot": snapshot, "ruler": ruler, "candidate": candidate, "decide": decide}[a.cmd](a)


if __name__ == "__main__":
    main()
