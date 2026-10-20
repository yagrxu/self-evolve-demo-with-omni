"""把一条 trace 的 span（云端 @record 或本地 search_local_telemetry 的 span）转成
manage_evaluations(run, targets=…) 的内联评分项。

为什么走 targets：2026-10-19 实测 `run` + `dataSource:"cloud"` + traceIds 对本 store
的每条 trace 都返回 "Failed to retrieve trace details …: UnknownError"。
targets 让**生产 trace 与本地 replay 走同一条评分通路** —— 同一把尺子量所有 arm。
"""
from __future__ import annotations

import json


def _events(span: dict) -> list[dict]:
    return span.get("events") or []


def _text(content) -> str:
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content)


def from_spans(spans: list[dict], key: str, example: dict | None = None) -> dict:
    """spans: 带 name / events(attributes) 的 OTel span 列表。"""
    user, output, system = "", "", ""
    tool_calls, contexts = [], []
    for s in spans:
        name = s.get("name", "")
        evs = _events(s)
        if name.startswith("invoke_agent"):
            for e in evs:
                a = e.get("attributes") or {}
                if e.get("name") == "gen_ai.user.message":
                    user = _text(a.get("content"))
                elif e.get("name") == "gen_ai.system.message":
                    system = _text(a.get("content"))
                elif e.get("name") == "gen_ai.choice":
                    output = _text(a.get("message"))
        elif name.startswith("execute_tool "):
            params, result = {}, ""
            for e in evs:
                a = e.get("attributes") or {}
                if e.get("name") == "gen_ai.tool.message":
                    try:
                        params = json.loads(a.get("content") or "{}")
                    except ValueError:
                        params = {"raw": a.get("content")}
                elif e.get("name") == "gen_ai.choice":
                    result = _text(a.get("message"))
            tool_calls.append({"name": name.split(" ", 1)[1], "parameters": params,
                               "output": result})
            contexts.append(result)
    # targets 模式下判官的 {context} 只看得到 input —— 实测 Faithfulness / PolicyGrounding
    # 因此报「上下文中未见工具调用记录」。把工具调用与返回附在 input 后面，
    # 所有 arm 用同一种拼法，尺子对所有 arm 一致。
    if tool_calls:
        log = "\n".join(f"- {c['name']}({json.dumps(c['parameters'], ensure_ascii=False)}) → "
                        f"{c['output']}" for c in tool_calls)
        judged_input = f"{user}\n\n[本轮 agent 的工具调用及返回]\n{log}"
    else:
        judged_input = f"{user}\n\n[本轮 agent 没有调用任何工具]"
    t = {"key": key, "input": judged_input, "output": output,
         "retrievedContexts": contexts, "toolCalls": tool_calls}
    if example:
        t["expected"] = example["turns"][0]["expected_response"]
        t["assertions"] = example.get("assertions", [])
        t["expectedTrajectory"] = example.get("expected_trajectory", [])
    t["_system"] = system
    return t


def local_to_otel(trace: dict) -> list[dict]:
    """把 search_local_telemetry(get) 的 span 形态转成云端 @record 的 OTel 形态，
    让 from_spans 对两边**逐字节同样地**拼出评分输入（同一把尺子）。"""
    out = []
    for s in trace.get("spans", []):
        name = s.get("name", "")
        evs = []
        if name.startswith("invoke_agent"):
            evs.append({"name": "gen_ai.user.message",
                        "attributes": {"content": json.dumps([{"text": s.get("input") or ""}],
                                                             ensure_ascii=False)}})
            evs.append({"name": "gen_ai.choice", "attributes": {"message": s.get("output") or ""}})
        elif name.startswith("execute_tool "):
            inp = s.get("input")
            evs.append({"name": "gen_ai.tool.message",
                        "attributes": {"content": inp if isinstance(inp, str)
                                       else json.dumps(inp or {}, ensure_ascii=False)}})
            evs.append({"name": "gen_ai.choice",
                        "attributes": {"message": json.dumps([{"text": s.get("output") or ""}],
                                                             ensure_ascii=False)}})
        out.append({"name": name, "events": evs})
    return out


def strip_private(t: dict) -> dict:
    return {k: v for k, v in t.items() if not k.startswith("_")}
