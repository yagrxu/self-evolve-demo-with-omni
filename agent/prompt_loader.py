"""Omni 托管 prompt 的加载器。

为什么必须有这一层
------------------
``omni-self-evolution`` skill 的硬性要求（Step 1.5 / Step 7.5）：

  > Confirm a baseline can be selected, **variants are isolated by explicit
  > version/override**, and **traces record prompt version/hash**.

也就是说 A/B replay 要成立，必须做到两件事：

1. **变体可隔离** —— 换 prompt 版本不能靠改代码。这里用环境变量
   ``OMNI_PROMPTS_OVERRIDE`` 指向另一份 prompts.json 实现：
   Phase 5 会为每个 candidate 生成一份独立的 prompts.json，
   然后用不同的 env 启动同一份代码，从而保证"除了 prompt 什么都没变"。

2. **trace 里能验证到底跑的是哪个版本** —— 由 ``OmniPromptProcessor``
   这个 SpanProcessor 在每个 span 上打 ``llm.prompt_template.version``。
   如果这个属性缺失，replay 结果就无法归因，skill 要求直接 ``NO_DECISION``。

结构基本照搬 ``omni-prompt-setup`` skill 给的参考实现（保持与 Omni Studio
Prompt Management 面板的 schema 兼容），额外加了 ``get_messages()``，
因为本项目的 agent 需要完整消息列表而不只是 system 文本。
"""

from __future__ import annotations

import hashlib
import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace import SpanProcessor

_cache: dict[str, Any] | None = None
_cache_mtime: float = 0.0
# (template, version_id) —— 由 get_prompt 写入，OmniPromptProcessor 读出打到 span 上
_active: ContextVar[tuple[str, str] | None] = ContextVar("active", default=None)


def prompts_path() -> Path:
    """当前生效的 prompts.json 路径。

    ``OMNI_PROMPTS_OVERRIDE`` 是变体隔离的钩子：Omni Studio 用它做热加载，
    我们在 Phase 5 的 paired replay 里用它切 candidate。
    """
    override = os.environ.get("OMNI_PROMPTS_OVERRIDE")
    return Path(override) if override else Path(__file__).parent / "prompts.json"


def _load() -> dict[str, Any]:
    global _cache, _cache_mtime
    p = prompts_path()
    if p.exists():
        mtime = p.stat().st_mtime
        if _cache is None or mtime != _cache_mtime:
            _cache = json.loads(p.read_text(encoding="utf-8"))
            _cache_mtime = mtime
    elif _cache is None:
        _cache = {}
    return _cache


def _version_id(name: str, data: dict[str, Any]) -> str:
    """解析当前生效的版本号。

    与 skill 的参考实现一致：取 history 最后一条的 versionId；如果 ``messages``
    已经被面板改过但还没提交成新版本，就加 ``-draft`` 后缀 —— 这样 trace 上
    看到的版本号能诚实反映"跑的其实是未提交的草稿"。
    """
    history = data.get("history", [])
    if not history:
        return f"{name}-v1"
    ver = history[-1].get("versionId", f"{name}-v{len(history)}")
    latest = history[-1].get("messages")
    if latest is not None:
        compare = data.get("_templateMessages", data.get("messages", []))
        norm = lambda ms: [(m.get("role"), m.get("content")) for m in ms]  # noqa: E731
        if norm(compare) != norm(latest):
            ver += "-draft"
    return ver


def get_messages(name: str) -> list[dict[str, str]]:
    """返回完整消息列表，并把 (template, version) 记入当前上下文。"""
    data = _load().get(name, {})
    msgs = data.get("messages", [])
    template = "\n".join(f"[{m['role']}]: {m['content']}" for m in msgs)
    _active.set((template, _version_id(name, data)))
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


def get_prompt(name: str) -> str:
    """返回 system prompt 文本（顺带记录版本，供 OmniPromptProcessor 使用）。"""
    msgs = get_messages(name)
    return next((m["content"] for m in msgs if m["role"] == "system"), "")


def get_model_config(name: str) -> dict[str, Any] | None:
    """返回 ``{"providerId","modelId","parameters"}``，没配则 None。

    调用方**必须**真的用它来实例化模型。skill 里专门警告过：只生成这个函数
    而把模型写死，会让 prompts.json 的 model 字段变成纯装饰 ——
    在 Omni 面板里换模型看着生效了，运行时零影响。
    Phase 5 的"换模型"这条优化轴完全依赖这里。
    """
    return _load().get(name, {}).get("model")


def get_prompt_version(name: str) -> str:
    return _version_id(name, _load().get(name, {}))


def get_prompt_hash(name: str) -> str:
    """当前 prompt 内容的稳定哈希。

    用 blake2b 而非内置 hash()（后者受 PYTHONHASHSEED 随机化影响，跨进程不稳定）。
    skill 要求把 baseline hash 冻结进 manifest，并在写回 winner 前校验文件未被
    第三方改动 —— 这个函数就是那个校验依据。
    """
    data = _load().get(name, {})
    payload = json.dumps(
        {"messages": data.get("messages", []), "model": data.get("model")},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def get_active() -> tuple[str, str] | None:
    return _active.get()


class OmniPromptProcessor(SpanProcessor):
    """把 prompt template / version 打到每个 span 上。

    必须注册进 TracerProvider，否则 trace 里不会有
    ``llm.prompt_template.version``，A/B replay 就无法验证"这条结果到底是哪个
    变体产生的" —— 那是 skill 明令要求 NO_DECISION 的情形。
    """

    def on_start(self, span, parent_context=None):  # noqa: D102
        active = _active.get()
        if active and span.is_recording():
            span.set_attribute("llm.prompt_template.template", active[0])
            span.set_attribute("llm.prompt_template.version", active[1])

    def on_end(self, span):  # noqa: D102
        pass

    def shutdown(self):  # noqa: D102
        pass

    def force_flush(self, timeout_millis: int | None = None):  # noqa: D102
        return True
