"""订单售后客服 agent —— Strands + Bedrock AgentCore Runtime。

同一份代码在三个地方跑，且行为一致（这是 A/B 可比的前提）：
  1. 本地 dev server（Phase 1/2/5）—— ``python agent.py``，Omni 注入 OTLP 端点
  2. AgentCore Runtime（Phase 3/6）—— Direct Code Deploy，entryPoint
     ``["opentelemetry-instrument", "agent.py"]``
  3. Phase 5 的 paired replay —— 靠 ``OMNI_PROMPTS_OVERRIDE`` 切 prompt 变体

三条可变轴（Phase 5 的三个 candidate）全部由**外部配置**驱动，代码零改动：
  · prompt   → ``OMNI_PROMPTS_OVERRIDE`` 指向不同的 prompts.json
  · model    → prompts.json 里的 ``model.modelId``（经 get_model_config 真实生效）
  · tools    → ``AGENT_TOOL_TIER=enhanced`` 追加计算类工具

这样才能满足 omni-self-evolution 的"除被测轴之外，model/tools/code/dataset 全部冻结"。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

import tools as T
from prompt_loader import (
    OmniPromptProcessor,
    get_model_config,
    get_prompt,
    get_prompt_hash,
    get_prompt_version,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("order-support")

PROMPT_NAME = "order_support"
# 兜底模型：只有 prompts.json 没配 model 时才会用到。
# skill 明确要求"永远保留原硬编码模型作为 fallback，不要丢掉"。
FALLBACK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ── 埋点：把 OmniPromptProcessor 挂到全局 TracerProvider ────────────────────

_processor_registered = False


def ensure_prompt_processor() -> bool:
    """让 trace 带上 ``llm.prompt_template.version``。幂等，可重复调用。

    为什么"拿已存在的 provider 再挂"而不是自己建一个
    ------------------------------------------------
    ADOT 自动埋点（entryPoint 前缀 ``opentelemetry-instrument``）会在业务代码
    **之前**建好 TracerProvider。自己再建一个会覆盖掉 ADOT 的导出管道，
    trace 就上不了云。所以只能往已有的 provider 上追加 SpanProcessor。

    为什么要惰性重试
    ----------------
    实测（见 docs/BUILD-LOG.md Phase 1.14）：
      · 有 ``opentelemetry-instrument``  → import 时 provider 已是 SDK
        ``TracerProvider``，一次就注册成功；
      · 裸 ``python agent.py``          → provider 是 ``ProxyTracerProvider``，
        没有 ``add_span_processor``，注册失败。
    而 ProxyTracerProvider 是会被后来的 ``set_tracer_provider`` 替换掉的 ——
    所以在每次 invoke 时重试一下，真 provider 一出现就挂上去，
    不至于因为加载顺序不同而永久丢掉版本属性（那会让 A/B 结论作废）。
    """
    global _processor_registered
    if _processor_registered:
        return True
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        # 不同 OTel 版本里 ProxyTracerProvider 解包方式不一样（get_delegate / _delegate /
        # 都没有），所以逐个试，最后直接看有没有 add_span_processor。
        real = provider
        for attr in ("get_delegate", "_delegate"):
            candidate = getattr(provider, attr, None)
            if callable(candidate):
                real = candidate()
                break
            if candidate is not None:
                real = candidate
                break

        if hasattr(real, "add_span_processor"):
            real.add_span_processor(OmniPromptProcessor())
            _processor_registered = True
            log.info("OmniPromptProcessor 已注册到 %s", type(real).__name__)
            return True
        log.warning(
            "TracerProvider 目前是 %s，不支持 add_span_processor —— 暂时不会有 "
            "llm.prompt_template.version；下次 invoke 会再试一次。"
            "若持续如此，说明启动命令没有走 opentelemetry-instrument / ADOT PYTHONPATH。",
            type(real).__name__,
        )
    except Exception:  # noqa: BLE001 - 埋点失败不能让 agent 挂掉
        log.exception("注册 OmniPromptProcessor 失败")
    return False


# ── 工具（薄包装，业务逻辑在 tools.py） ────────────────────────────────────


@tool
def get_order(order_id: str) -> str:
    """查询订单原始信息：下单日期、今天日期、商品清单（SKU/名称/类目/原价/数量/是否破损）、
    运费、优惠券。

    注意：本工具不计算「距今多少天」，需要你自己用 order_date 和 today 相减。

    Args:
        order_id: 订单号，格式如 ORD-10001
    """
    return json.dumps(T.get_order(order_id), ensure_ascii=False)


@tool
def get_refund_policy(category: str) -> str:
    """查询某个商品类目的官方退换货政策字段：退货窗口天数、折旧费比例、
    寄回运费承担方、超窗口后能否换新及换新窗口天数；并附优惠券分摊规则、
    运费退还规则、破损特殊规则的条款原文。

    Args:
        category: 商品类目，可选值 electronics / apparel / books / home / perishable
    """
    return json.dumps(T.get_refund_policy(category), ensure_ascii=False)


@tool
def search_knowledge_base(query: str) -> str:
    """检索客服 FAQ 知识库，返回与关键词相关的常见问题解答。

    Args:
        query: 检索关键词，如「退货流程」「运费谁承担」「破损」
    """
    return json.dumps(T.search_knowledge_base(query), ensure_ascii=False)


@tool
def check_return_eligibility(order_id: str, reason: str = "") -> str:
    """判定订单中每件商品当前是否可退款、只能换新、还是完全不可退，
    并给出依据的政策条款 id 和替代方案。

    Args:
        order_id: 订单号
        reason: 用户陈述的退货原因（可选，如"不喜欢"、"破损"、"尺码不合"）
    """
    return json.dumps(T.check_return_eligibility(order_id, reason), ensure_ascii=False)


@tool
def calculate_refund(order_id: str, items_json: str = "") -> str:
    """计算精确退款金额，逐项给出：原价小计、分摊的优惠券金额、分摊后实付、
    折旧费、该项退款额，以及原始运费是否退还及依据。

    Args:
        order_id: 订单号
        items_json: 要退的商品，JSON 数组如 '[{"sku":"PC-TPU","qty":1}]'。
                    留空表示整单退货。
    """
    items = json.loads(items_json) if items_json.strip() else None
    return json.dumps(T.calculate_refund(order_id, items), ensure_ascii=False)


@tool
def create_return_label(order_id: str, skus_json: str = "") -> str:
    """为可退商品生成退货单号 (RMA) 和寄回说明。只对判定为可退的商品生效。

    Args:
        order_id: 订单号
        skus_json: 要退的 SKU，JSON 数组如 '["PC-TPU"]'。留空表示全部可退商品。
    """
    skus = json.loads(skus_json) if skus_json.strip() else None
    return json.dumps(T.create_return_label(order_id, skus), ensure_ascii=False)


@tool
def get_coupon_allocation(order_id: str) -> str:
    """查询订单级优惠券在各商品间的分摊明细（每件商品占原价的比例、分摊到的券额、
    分摊后实付金额）。

    Args:
        order_id: 订单号
    """
    return json.dumps(T.get_coupon_allocation(order_id), ensure_ascii=False)


# BASIC 层：只有原始数据 + 一个干扰项 FAQ + 落单动作。
# 推理（日期减法、套政策、判边界）和算术（券分摊、折旧费、运费）全部落在模型头上。
BASIC_TOOLS = [
    get_order,
    get_refund_policy,
    search_knowledge_base,
    create_return_label,
]

# ENHANCED 层额外提供的计算类工具。对应 Phase 5 candidate c2「加 tool」。
ENHANCED_EXTRA_TOOLS = [
    check_return_eligibility,
    calculate_refund,
    get_coupon_allocation,
]


def tool_tier() -> str:
    """``basic``（默认，v1 baseline）或 ``enhanced``（Phase 5 candidate c2）。"""
    tier = os.environ.get("AGENT_TOOL_TIER", "basic").strip().lower()
    if tier not in ("basic", "enhanced"):
        log.warning("AGENT_TOOL_TIER=%r 无效，回退到 basic", tier)
        return "basic"
    return tier


def active_tools() -> list:
    """当前变体启用的工具集。

    这是 Phase 5「加 tool」这条优化轴的唯一开关。默认 basic ——
    所以 baseline 和纯 prompt 类 candidate（c1 / c3）用的是**完全相同**的 4 个工具，
    满足 skill 要求的"除被测轴之外一切冻结"。
    """
    return list(BASIC_TOOLS) + (list(ENHANCED_EXTRA_TOOLS) if tool_tier() == "enhanced" else [])


# ── Agent 构造 ─────────────────────────────────────────────────────────────


def build_agent() -> Agent:
    """每次调用都新建 Agent —— 让 prompts.json 的热改动立即生效（Omni 面板需要），
    也保证每个 replay 会话之间没有隐式状态串味。
    """
    from strands.models import BedrockModel

    system_prompt = get_prompt(PROMPT_NAME)  # 顺带把 version 写入 ContextVar
    cfg = get_model_config(PROMPT_NAME)
    params = (cfg or {}).get("parameters", {}) or {}

    model = BedrockModel(
        model_id=(cfg or {}).get("modelId", FALLBACK_MODEL_ID),
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
        temperature=params.get("temperature", 0.3),
        max_tokens=params.get("max_tokens", 1024),
    )
    return Agent(model=model, system_prompt=system_prompt, tools=active_tools())


app = BedrockAgentCoreApp()
ensure_prompt_processor()


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AgentCore ``/invocations`` 入口。

    请求体契约与 Omni 的 ``AgentCore`` preset 一致：
    ``{"prompt": "...", "sessionId": "..."}``
    """
    # 惰性重试：ProxyTracerProvider 可能在 import 之后才被真 provider 替换
    prompt_attrs_ok = ensure_prompt_processor()

    user_prompt = (payload or {}).get("prompt", "")
    session_id = (payload or {}).get("sessionId", "")
    if not user_prompt:
        return {"error": "payload 缺少 prompt 字段"}

    version = get_prompt_version(PROMPT_NAME)
    log.info(
        "invoke session=%s prompt_version=%s hash=%s tier=%s tools=%d",
        session_id, version, get_prompt_hash(PROMPT_NAME)[:12], tool_tier(), len(active_tools()),
    )

    result = build_agent()(user_prompt)

    return {
        "result": str(result),
        # 回显变体身份：Phase 5 用它做二次校验，防止 trace 属性缺失时静默错配
        "prompt_version": version,
        "prompt_hash": get_prompt_hash(PROMPT_NAME),
        "model_id": (get_model_config(PROMPT_NAME) or {}).get("modelId", FALLBACK_MODEL_ID),
        "tool_tier": tool_tier(),
        "tools_enabled": [t.__name__ if hasattr(t, "__name__") else str(t) for t in active_tools()],
        "sessionId": session_id,
        # 告诉调用方 trace 上到底有没有 prompt 版本属性。
        # 为 false 时 A/B replay 不能靠 trace 归因变体，必须退回用这里回显的
        # prompt_version/prompt_hash —— 别让它静默失败。
        "prompt_attrs_on_trace": prompt_attrs_ok,
    }


if __name__ == "__main__":
    app.run()
