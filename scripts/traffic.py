#!/usr/bin/env python3
"""向已部署的 AgentCore Runtime 打模拟流量。

Phase 3 用 ``prod_sim``（模拟真实用户），Phase 6 用 ``verify``（held-out 验证）。

核心设计：sessionId 里显式编码 scenario_id
------------------------------------------
``omni-self-evolution`` SKILL.md 的 Step 6 有一条硬性约束（大意）：

  > Do **not** rely on production ``sourceTraceId``, replay trace ID equality,
  > or **positional dataset matching**.

也就是说：不能靠"第 3 条 trace 对应第 3 条 dataset"这种脆弱假设去连接
云端 trace 和标注答案 —— 云端 trace 的返回顺序不保证，采样也可能丢条。

所以这里把 scenario_id 编进 ``sessionId``：
  ``selfevolve-<dataset>-<scenario_id>-<run_id>``
云端的 session 列是 ``attributes.session.id``（可直接 SQL 过滤），
于是"哪条 trace 对应哪条 ground truth"变成一个确定性的字符串解析，
而不是猜。同时脚本还会把完整映射落盘到 ``runs/<run_id>/traffic.jsonl``，双保险。

用法：
    python scripts/traffic.py --dataset prod_sim
    python scripts/traffic.py --dataset verify --endpoint v2
    python scripts/traffic.py --dataset prod_sim --limit 3 --dry-run
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGION = "us-west-2"  # 显式，且与 Omni Space 同 region：见 docs/BUILD-LOG.md 决策点 R4/R4'
AGENT_STACK = "selfevolve-demo-agent"

# AgentCore 的 runtimeSessionId 有最小长度要求（33 字符）。
# 我们的命名天然够长，但短 scenario_id 时仍会补齐，免得踩到 ValidationException。
MIN_SESSION_ID_LEN = 33


def resolve_runtime(stack_name: str, endpoint: str) -> tuple[str, str]:
    """从 CloudFormation 输出里取 runtime ARN 和 log group。

    不硬编码 ARN：Phase 6 重新部署后 runtime 版本会变，读 stack 输出永远是对的。
    """
    import boto3

    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except cfn.exceptions.ClientError as e:
        raise SystemExit(
            f"读不到 stack {stack_name}：{e}\n"
            f"还没部署？先跑：scripts/build_agent_bundle.sh && cd cdk && npm run deploy"
        ) from e
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    arn = outputs.get("RuntimeArn")
    if not arn:
        raise SystemExit(f"stack {stack_name} 没有 RuntimeArn 输出，输出项为：{sorted(outputs)}")
    # log group 必须**随 qualifier 走**。stack 输出的 ApplicationLogGroup 指向
    # `...-DEFAULT`，而我们用 endpoint `v1` 调用时 trace 实际落在 `...-v1`。
    # 记错了不会报错，只会让后续查 log 的人查一个空 group（实测踩过，见 BUILD-LOG 3.4）。
    default_lg = outputs.get("ApplicationLogGroup", "")
    runtime_id = arn.rsplit("/", 1)[-1]
    log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{endpoint}" if runtime_id else default_lg
    return arn, log_group


def make_session_id(dataset: str, scenario_id: str, run_id: str) -> str:
    sid = f"selfevolve-{dataset}-{scenario_id}-{run_id}"
    if len(sid) < MIN_SESSION_ID_LEN:
        sid = sid + "-" + "0" * (MIN_SESSION_ID_LEN - len(sid) - 1)
    return sid


def parse_session_id(session_id: str) -> dict[str, str]:
    """make_session_id 的逆运算 —— 导出 trace 时用它把 trace 连回 scenario。"""
    parts = session_id.split("-")
    if len(parts) < 4 or parts[0] != "selfevolve":
        return {}
    return {"dataset": parts[1], "scenario_id": "-".join(parts[2:-1]), "run_id": parts[-1]}


def read_response(resp: dict) -> str:
    """把 invoke_agent_runtime 的响应体读成文本。

    AgentCore 的响应体可能是普通 bytes 流，也可能是 SSE 分片（取决于 Accept 头
    和 agent 是否流式返回），所以这里两种都兜住。
    """
    body = resp.get("response")
    if body is None:
        return ""
    raw = body.read() if hasattr(body, "read") else bytes(body)
    text = raw.decode("utf-8", errors="replace")
    if "data:" not in text:
        return text
    chunks = [
        line[len("data:"):].strip()
        for line in text.splitlines()
        if line.startswith("data:")
    ]
    return "\n".join(c for c in chunks if c and c != "[DONE]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, choices=["dev_local", "prod_sim", "verify"])
    ap.add_argument("--endpoint", default="v1", help="RuntimeEndpoint 名（qualifier）")
    ap.add_argument("--stack", default=AGENT_STACK)
    ap.add_argument("--limit", type=int, default=0, help="只发前 N 条（调试用）")
    ap.add_argument("--min-gap", type=float, default=1.5, help="请求间最小间隔秒")
    ap.add_argument("--max-gap", type=float, default=6.0, help="请求间最大间隔秒")
    ap.add_argument("--seed", type=int, default=20260917, help="间隔抖动的随机种子（保证可复现）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不真发")
    args = ap.parse_args()

    ds_file = REPO / "datasets" / f"{args.dataset}.json"
    if not ds_file.exists():
        raise SystemExit(f"{ds_file} 不存在。先跑：python scripts/build_datasets.py")
    examples = json.loads(ds_file.read_text(encoding="utf-8"))["examples"]
    if args.limit:
        examples = examples[: args.limit]

    run_id = uuid.uuid4().hex[:8]
    started = datetime.now(timezone.utc)
    out_dir = REPO / "runs" / f"traffic-{args.dataset}-{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"dataset={args.dataset}  endpoint={args.endpoint}  条数={len(examples)}  run_id={run_id}")

    if args.dry_run:
        for e in examples:
            print(f"  {e['scenario_id']:10s} session={make_session_id(args.dataset, e['scenario_id'], run_id)}")
            print(f"    -> {e['turns'][0]['input']}")
        return 0

    import boto3

    runtime_arn, log_group = resolve_runtime(args.stack, args.endpoint)
    print(f"runtime={runtime_arn}\nlog_group={log_group}\n")

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    rng = random.Random(args.seed)
    records, ok, failed = [], 0, 0

    for i, ex in enumerate(examples, start=1):
        scenario_id = ex["scenario_id"]
        session_id = make_session_id(args.dataset, scenario_id, run_id)
        user_input = ex["turns"][0]["input"]
        # 与 Omni 的 AgentCore preset 完全一致的请求体契约
        payload = {"prompt": user_input, "sessionId": session_id}

        t0 = time.time()
        rec: dict = {
            "scenario_id": scenario_id,
            "session_id": session_id,
            "dataset": args.dataset,
            "endpoint": args.endpoint,
            "run_id": run_id,
            "archetype": ex["metadata"]["archetype"],
            "order_id": ex["metadata"]["order_id"],
            "input": user_input,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            resp = client.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=session_id,
                qualifier=args.endpoint,
                contentType="application/json",
                accept="application/json",
                payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            text = read_response(resp)
            rec.update({
                "status": "ok",
                "latency_ms": round((time.time() - t0) * 1000),
                "output": text,
                # runtime 会回显 prompt_version / prompt_hash（见 agent.py 的 invoke），
                # 这是"这条流量到底跑的哪个变体"的第二重证据，防止 trace 属性缺失
                "echoed": _extract_echo(text),
                "trace_id_header": resp.get("ResponseMetadata", {}).get("RequestId"),
            })
            ok += 1
            print(f"  [{i:3d}/{len(examples)}] ✓ {scenario_id} {rec['latency_ms']:>6}ms  {rec['echoed'].get('prompt_version','?')}")
        except Exception as e:  # noqa: BLE001 —— 单条失败不能中断整轮流量
            rec.update({"status": "error", "latency_ms": round((time.time() - t0) * 1000),
                        "error": f"{type(e).__name__}: {e}"})
            failed += 1
            print(f"  [{i:3d}/{len(examples)}] ✗ {scenario_id}  {rec['error']}")

        records.append(rec)
        if i < len(examples):
            time.sleep(rng.uniform(args.min_gap, args.max_gap))

    finished = datetime.now(timezone.utc)

    # 落盘：这份 manifest 是后续所有分析的连接键，比 trace 本身更重要
    (out_dir / "traffic.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    manifest = {
        "run_id": run_id,
        "dataset": args.dataset,
        "endpoint": args.endpoint,
        "runtime_arn": runtime_arn,
        "log_group": log_group,
        "region": REGION,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        # 云端 SQL 查询要用的整数秒边界（search_agent_traces 要求 to_timestamp(整数秒)，
        # 两端都必须绑定 —— 见 BUILD-LOG Phase -1.6 第 1 条）
        "window_start_epoch_s": int(started.timestamp()) - 60,
        "window_end_epoch_s": int(finished.timestamp()) + 900,
        "counts": {"total": len(records), "ok": ok, "error": failed},
        "session_to_scenario": {r["session_id"]: r["scenario_id"] for r in records},
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n成功 {ok} / 失败 {failed}，产物：{out_dir}")
    print(f"云端查询窗口（整数秒）：{manifest['window_start_epoch_s']} .. {manifest['window_end_epoch_s']}")
    print("trace 落到云端通常还要几分钟，之后跑：python scripts/fetch_cloud_scores.py --run", out_dir.name)
    return 0 if failed == 0 else 1


def _extract_echo(text: str) -> dict:
    """从 agent 返回里抠出 prompt_version / prompt_hash / model_id / tools_enabled。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("prompt_version", "prompt_hash", "model_id", "tools_enabled") if k in data}


if __name__ == "__main__":
    sys.exit(main())
