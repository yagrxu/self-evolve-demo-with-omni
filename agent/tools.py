"""订单售后客服 agent 的工具层。

设计约束（都是为了让评估可信，不是为了"像真的"）
------------------------------------------------
1. **完全确定性。** 没有网络调用、没有随机数、没有 `date.today()`。
   "今天"由 ``AS_OF_DATE`` 固定（env ``DEMO_AS_OF_DATE`` 可覆盖）。
   这样同一条 dataset example 在任何时间跑都得到同一个 oracle ——
   否则 A/B replay 的 baseline 和 candidate 会因为跑的时刻不同而不可比。

2. **无外部副作用。** ``create_return_label`` 只写进程内的 dict。
   ``omni-self-evolution`` skill 要求 replay 前必须能*证明*隔离性
   （"Treat unknown environments as production"），纯内存 fixture 是最强的证明。

3. **业务逻辑是纯函数，工具是薄包装。**
   ``scripts/build_datasets.py`` 直接 import 这些纯函数来算 dataset 的
   ``expected_response``，所以 oracle 和运行时用的是**同一份**算法 ——
   不会出现"标注答案本身就是错的"这种最恶劣的评估污染。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"

# ── fixture 加载（进程内缓存一次） ──────────────────────────────────────────

_orders_cache: dict[str, dict] | None = None
_policies_cache: dict[str, Any] | None = None


def _orders() -> dict[str, dict]:
    global _orders_cache
    if _orders_cache is None:
        raw = json.loads((FIXTURES / "orders.json").read_text(encoding="utf-8"))
        _orders_cache = {o["order_id"]: o for o in raw["orders"]}
    return _orders_cache


def _policies() -> dict[str, Any]:
    global _policies_cache
    if _policies_cache is None:
        _policies_cache = json.loads((FIXTURES / "policies.json").read_text(encoding="utf-8"))
    return _policies_cache


def as_of() -> date:
    """本 Demo 的"今天"。固定值 —— 见模块 docstring 第 1 条。"""
    return datetime.strptime(os.environ.get("DEMO_AS_OF_DATE", "2026-09-17"), "%Y-%m-%d").date()


def _money(x: float) -> float:
    """四舍五入到分。金额一律经过这里，避免浮点尾差进入 oracle。"""
    return round(x + 1e-9, 2)


# ── 纯业务逻辑 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ItemMoney:
    """一件商品的金额拆解（含优惠券分摊结果）。"""

    sku: str
    name: str
    category: str
    list_price: float
    qty: int
    damaged: bool
    list_subtotal: float       # 原价小计 = list_price * qty
    coupon_allocated: float    # 该商品分摊到的券金额
    paid_subtotal: float       # 实付小计 = list_subtotal - coupon_allocated


def allocate_coupon(order_id: str) -> list[ItemMoney]:
    """把订单级优惠券按原价小计比例分摊到每件商品。

    实现的是 policies.json 里的 ``COUPON_PRORATE_BY_LIST_PRICE``：
    先按比例四舍五入到分，再把因舍入产生的差额补给原价小计最大的那件商品，
    保证 ``sum(coupon_allocated) == coupon.amount``（分毫不差）。

    这是整个 Demo 最关键的算术 oracle —— v1 agent 会自己瞎算，而且必然算错。
    """
    order = _orders()[order_id]
    coupon_amount = float(order["coupon"]["amount"]) if order.get("coupon") else 0.0

    rows = []
    for it in order["items"]:
        rows.append(
            {
                **it,
                "list_subtotal": _money(float(it["list_price"]) * int(it["qty"])),
            }
        )
    list_total = _money(sum(r["list_subtotal"] for r in rows))

    if coupon_amount > 0 and list_total > 0:
        for r in rows:
            r["coupon_allocated"] = _money(coupon_amount * r["list_subtotal"] / list_total)
        # 舍入差额补给原价小计最大的那件
        drift = _money(coupon_amount - sum(r["coupon_allocated"] for r in rows))
        if abs(drift) >= 0.005:
            biggest = max(rows, key=lambda r: r["list_subtotal"])
            biggest["coupon_allocated"] = _money(biggest["coupon_allocated"] + drift)
    else:
        for r in rows:
            r["coupon_allocated"] = 0.0

    return [
        ItemMoney(
            sku=r["sku"],
            name=r["name"],
            category=r["category"],
            list_price=float(r["list_price"]),
            qty=int(r["qty"]),
            damaged=bool(r["damaged"]),
            list_subtotal=r["list_subtotal"],
            coupon_allocated=r["coupon_allocated"],
            paid_subtotal=_money(r["list_subtotal"] - r["coupon_allocated"]),
        )
        for r in rows
    ]


def days_since_purchase(order_id: str) -> int:
    order = _orders()[order_id]
    return (as_of() - datetime.strptime(order["order_date"], "%Y-%m-%d").date()).days


def item_verdict(order_id: str, sku: str) -> dict[str, Any]:
    """单件商品的资格判定。返回结构化裁决，含依据的政策条款 id。

    ``policy_basis`` 字段是故意加的：它让"agent 有没有真的读政策"变成可验证的 ——
    正确回答必须能对上这里的条款 id，编出来的政策对不上。
    """
    days = days_since_purchase(order_id)
    money = {m.sku: m for m in allocate_coupon(order_id)}[sku]
    pol = _policies()["categories"][money.category]

    if money.damaged:
        return {
            "sku": sku, "name": money.name, "category": money.category, "days_since_purchase": days,
            "verdict": "ELIGIBLE_FULL_REFUND",
            "policy_basis": "DAMAGED_OVERRIDE",
            "reason": f"商品到货破损，依据 DAMAGED_OVERRIDE 条款可全额退款，免 {pol['restocking_fee_pct']}% 折旧费，运费由商家承担（已超窗口也适用）。",
            "restocking_fee_waived": True,
        }
    if pol["return_window_days"] == 0:
        return {
            "sku": sku, "name": money.name, "category": money.category, "days_since_purchase": days,
            "verdict": "NOT_ELIGIBLE",
            "policy_basis": f"category:{money.category}",
            "reason": f"{pol['display_name']}不支持无理由退货（{pol['notes']}）。",
            "restocking_fee_waived": False,
        }
    if days <= pol["return_window_days"]:
        return {
            "sku": sku, "name": money.name, "category": money.category, "days_since_purchase": days,
            "verdict": "ELIGIBLE_REFUND",
            "policy_basis": f"category:{money.category}",
            "reason": f"下单 {days} 天，在 {pol['display_name']} {pol['return_window_days']} 天退货窗口内，可退款；按政策收 {pol['restocking_fee_pct']}% 折旧费，寄回运费由{'商家' if pol['return_shipping_paid_by'] == 'merchant' else '买家'}承担。",
            "restocking_fee_waived": False,
        }
    if pol["exchange_after_window"] and days <= pol["exchange_window_days"]:
        return {
            "sku": sku, "name": money.name, "category": money.category, "days_since_purchase": days,
            "verdict": "EXCHANGE_ONLY",
            "policy_basis": f"category:{money.category}",
            "reason": f"下单 {days} 天，已超出 {pol['return_window_days']} 天退货窗口，不可退款；但仍在 {pol['exchange_window_days']} 天换新窗口内，可换同款新品。",
            "restocking_fee_waived": False,
        }
    return {
        "sku": sku, "name": money.name, "category": money.category, "days_since_purchase": days,
        "verdict": "NOT_ELIGIBLE",
        "policy_basis": f"category:{money.category}",
        "reason": f"下单 {days} 天，已超出退货窗口（{pol['return_window_days']} 天）"
                  + (f"和换新窗口（{pol['exchange_window_days']} 天）" if pol["exchange_after_window"] else "")
                  + "，只能走厂商保修。",
        "restocking_fee_waived": False,
    }


def compute_refund(order_id: str, returns: list[dict[str, Any]]) -> dict[str, Any]:
    """算退款。``returns`` = ``[{"sku": ..., "qty": ...}, ...]``。

    公式（对应 policies.json 的三条 rule）：
      分摊后实付  = 原价小计 - 分摊券额          (COUPON_PRORATE_BY_LIST_PRICE)
      折旧费      = 分摊后实付 * pct%            (破损免收 → DAMAGED_OVERRIDE)
      退款        = 分摊后实付 - 折旧费
      运费退还    = 整单退且商家承担运费, 或破损  (SHIPPING_REFUND_RULE)
    """
    order = _orders()[order_id]
    money = {m.sku: m for m in allocate_coupon(order_id)}

    lines, total_refund, blocked = [], 0.0, []
    for req in returns:
        sku, qty = req["sku"], int(req["qty"])
        if sku not in money:
            blocked.append({"sku": sku, "error": f"订单 {order_id} 中不存在 SKU {sku}"})
            continue
        m = money[sku]
        if qty < 1 or qty > m.qty:
            blocked.append({"sku": sku, "error": f"退货数量 {qty} 超出购买数量 {m.qty}"})
            continue

        verdict = item_verdict(order_id, sku)
        if verdict["verdict"] not in ("ELIGIBLE_REFUND", "ELIGIBLE_FULL_REFUND"):
            blocked.append({"sku": sku, "verdict": verdict["verdict"], "reason": verdict["reason"]})
            continue

        pol = _policies()["categories"][m.category]
        returned_list = _money(m.list_price * qty)
        returned_coupon = _money(m.coupon_allocated * qty / m.qty)
        returned_paid = _money(returned_list - returned_coupon)
        fee_pct = 0.0 if verdict["restocking_fee_waived"] else float(pol["restocking_fee_pct"])
        restocking = _money(returned_paid * fee_pct / 100.0)
        refund = _money(returned_paid - restocking)

        lines.append({
            "sku": sku, "name": m.name, "category": m.category, "qty_returned": qty,
            "list_subtotal_returned": returned_list,
            "coupon_allocated_returned": returned_coupon,
            "paid_after_coupon": returned_paid,
            "restocking_fee_pct": fee_pct,
            "restocking_fee": restocking,
            "item_refund": refund,
        })
        total_refund = _money(total_refund + refund)

    # 运费：整单退 + 商家承担；或破损覆盖
    returning_all = bool(lines) and not blocked and sum(l["qty_returned"] for l in lines) == sum(
        m.qty for m in money.values()
    )
    any_damaged = any(money[l["sku"]].damaged for l in lines)
    shipping_paid_by = {_policies()["categories"][l["category"]]["return_shipping_paid_by"] for l in lines}
    shipping_refund = 0.0
    shipping_reason = "部分退货不退原始运费（SHIPPING_REFUND_RULE）。"
    if any_damaged:
        shipping_refund = _money(float(order["shipping_fee"]))
        shipping_reason = "商品破损，原始运费全额退还且寄回运费由商家承担（DAMAGED_OVERRIDE）。"
    elif returning_all and shipping_paid_by == {"merchant"}:
        shipping_refund = _money(float(order["shipping_fee"]))
        shipping_reason = "整单退货且该品类运费由商家承担，原始运费退还（SHIPPING_REFUND_RULE）。"
    elif returning_all:
        shipping_reason = "整单退货，但该品类寄回运费由买家承担，原始运费不退（SHIPPING_REFUND_RULE）。"

    return {
        "order_id": order_id,
        "currency": "CNY",
        "coupon": order.get("coupon"),
        "coupon_allocation_rule": _policies()["coupon_allocation_rule"]["id"],
        "lines": lines,
        "blocked": blocked,
        "items_refund_total": total_refund,
        "original_shipping_fee": _money(float(order["shipping_fee"])),
        "shipping_refund": shipping_refund,
        "shipping_reason": shipping_reason,
        "total_refund": _money(total_refund + shipping_refund),
        "is_full_order_return": returning_all,
    }


# ── 工具（薄包装；被 agent.py 注册给模型） ─────────────────────────────────

# create_return_label 的落库。进程内 dict —— 无外部副作用，见 docstring 第 2 条。
_RETURN_LABELS: dict[str, dict[str, Any]] = {}


# ── BASIC 层：只返回原始数据，不做任何推理或计算 ──────────────────────────
#
# 为什么工具要"弱"（这是刻意的设计，不是偷懒）
# ------------------------------------------
# 第一版工具做得太强：check_return_eligibility 直接返回含政策依据的完整裁决，
# calculate_refund 包办全部算术。结果实测发现 —— 即使 v1 那个极简 prompt，
# Haiku 也能把全部场景答对（包括最难的 44.88 优惠券分摊），因为它只需要
# "调一次工具、把结果念出来"。整个 Demo 的前提"一开始效果不好"就不成立了。
# 详见 docs/BUILD-LOG.md Phase 1.15。
#
# 所以 BASIC 层退回真实后端的样子：给原始字段，推理和算术留给模型。
# 计算类工具（check_return_eligibility / calculate_refund / get_coupon_allocation）
# 移到 ENHANCED 层，成为 Phase 5"加 tool"这条优化轴的实际内容 ——
# 这样那条轴才真正有效果，而不是装饰。


def get_order(order_id: str) -> dict[str, Any]:
    """查订单原始信息：下单日期、商品清单、类目、原价、数量、是否破损、运费、优惠券。

    **刻意不返回** ``days_since_purchase`` —— "距今多少天"要模型自己用
    ``order_date`` 和 ``today`` 相减。这是 v1 的一个真实失败来源：
    Haiku 做跨月日期减法（08-25 → 09-17 = 23 天）经常差一两天，
    而在窗口边界上差一天就会给出完全相反的结论。
    """
    order = _orders().get(order_id)
    if order is None:
        return {"error": f"订单 {order_id} 不存在", "hint": "订单号格式为 ORD-xxxxx"}
    return {
        "order_id": order["order_id"],
        "customer": order["customer"],
        "order_date": order["order_date"],
        "today": as_of().isoformat(),
        "shipping_fee": _money(float(order["shipping_fee"])),
        "coupon": order.get("coupon"),
        "items": [
            {"sku": it["sku"], "name": it["name"], "category": it["category"],
             "list_price": _money(float(it["list_price"])), "qty": int(it["qty"]),
             "damaged": bool(it["damaged"])}
            for it in order["items"]
        ],
    }


def get_refund_policy(category: str) -> dict[str, Any]:
    """查某个类目的原始政策字段 + 三条横切规则原文。

    只给字段和规则原文，**不给结论**。是否超窗口、该收多少折旧费、
    运费退不退，都要模型自己套用。
    """
    pol = _policies()["categories"].get(category)
    if pol is None:
        return {"error": f"未知类目 {category}",
                "valid_categories": sorted(_policies()["categories"].keys())}
    return {
        "category": category,
        "display_name": pol["display_name"],
        "return_window_days": pol["return_window_days"],
        "restocking_fee_pct": pol["restocking_fee_pct"],
        "return_shipping_paid_by": pol["return_shipping_paid_by"],
        "exchange_after_window": pol["exchange_after_window"],
        "exchange_window_days": pol["exchange_window_days"],
        "notes": pol["notes"],
        # 三条横切规则给原文。PolicyGrounding 判"有没有依据"时比对的就是这些。
        "coupon_allocation_rule": _policies()["coupon_allocation_rule"],
        "shipping_rule": _policies()["shipping_rule"],
        "damaged_rule": _policies()["damaged_rule"],
        "policy_version": _policies()["_version"],
    }


def search_knowledge_base(query: str) -> dict[str, Any]:
    """按关键词检索客服 FAQ 知识库。

    **这是一个干扰项工具**（见 fixtures/faq.json 的说明）。它返回的是真实企业
    帮助中心那种"措辞友好但没有可执行数值"的文本 —— 没说谎，但不精确、不分类目。

    v1 的 prompt 没有规定"政策数值必须来自 get_refund_policy"，
    所以模型很可能拿这里的"一般来说 7-15 天"当依据就下结论。
    这正是 ToolSelectionAccuracy 和 PolicyGrounding 要抓的失败模式。
    """
    faq = json.loads((FIXTURES / "faq.json").read_text(encoding="utf-8"))
    q = (query or "").lower()
    scored = []
    for e in faq["entries"]:
        hits = sum(1 for kw in e["keywords"] if kw.lower() in q or q in kw.lower())
        if hits:
            scored.append((hits, e))
    scored.sort(key=lambda x: -x[0])
    picked = [e for _, e in scored[:3]] or faq["entries"][:2]
    return {
        "query": query,
        "results": [{"id": e["id"], "title": e["title"], "content": e["content"]} for e in picked],
        "source": "customer-service-faq",
        "disclaimer": "FAQ 为通用说明，具体以订单适用的品类政策为准。",
    }


# ── ENHANCED 层：把推理与算术交给代码 ─────────────────────────────────────
#
# 这三个工具只在 AGENT_TOOL_TIER=enhanced 时注册（见 agent.py），
# 对应 Phase 5 的 candidate c2 —— 用户明确要求的"用不同的 tools 来提高能力"。


def check_return_eligibility(order_id: str, reason: str = "") -> dict[str, Any]:
    """判断订单里每件商品是否可退/可换，并给出依据的政策条款和替代方案。

    ENHANCED 层工具：把"日期减法 + 套政策 + 判边界"从模型手里拿走。
    """
    if order_id not in _orders():
        return {"error": f"订单 {order_id} 不存在"}
    verdicts = [item_verdict(order_id, m.sku) for m in allocate_coupon(order_id)]
    return {
        "order_id": order_id,
        "as_of_date": as_of().isoformat(),
        "days_since_purchase": days_since_purchase(order_id),
        "customer_stated_reason": reason,
        "items": verdicts,
        "summary": {
            "refundable": [v["sku"] for v in verdicts if v["verdict"].startswith("ELIGIBLE")],
            "exchange_only": [v["sku"] for v in verdicts if v["verdict"] == "EXCHANGE_ONLY"],
            "not_eligible": [v["sku"] for v in verdicts if v["verdict"] == "NOT_ELIGIBLE"],
        },
    }


def calculate_refund(order_id: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """算精确退款金额。``items`` 省略时按整单退计算。

    返回逐项拆解：原价小计 / 分摊券额 / 分摊后实付 / 折旧费 / 该项退款，
    以及运费是否退还及其依据。

    ENHANCED 层工具：把优惠券按比例分摊、按件拆分、扣折旧费这套算术
    从模型手里拿走。这是 v1 最主要的错误来源。
    """
    if order_id not in _orders():
        return {"error": f"订单 {order_id} 不存在"}
    if not items:
        items = [{"sku": m.sku, "qty": m.qty} for m in allocate_coupon(order_id)]
    return compute_refund(order_id, items)


def get_coupon_allocation(order_id: str) -> dict[str, Any]:
    """查订单级优惠券在各商品间的分摊明细。

    **这是 Phase 5 的 candidate c2 新增的工具**（"加 tool"这条优化轴）。
    目的：把优惠券按比例分摊这件算术从模型手里拿走、交给代码，
    因为基线的主要错误来源就是模型自己算分摊。
    v1/v2 不注册这个工具 —— 由 env ``ENABLE_COUPON_TOOL`` 控制（见 agent.py）。
    """
    if order_id not in _orders():
        return {"error": f"订单 {order_id} 不存在"}
    rows = allocate_coupon(order_id)
    order = _orders()[order_id]
    return {
        "order_id": order_id,
        "coupon": order.get("coupon"),
        "rule": _policies()["coupon_allocation_rule"],
        "list_price_total": _money(sum(r.list_subtotal for r in rows)),
        "allocations": [
            {"sku": r.sku, "name": r.name, "list_subtotal": r.list_subtotal,
             "share_pct": _money(100.0 * r.list_subtotal / sum(x.list_subtotal for x in rows)),
             "coupon_allocated": r.coupon_allocated, "paid_after_coupon": r.paid_subtotal}
            for r in rows
        ],
        "allocated_total": _money(sum(r.coupon_allocated for r in rows)),
    }


def create_return_label(order_id: str, skus: list[str] | None = None) -> dict[str, Any]:
    """为可退商品生成退货单号 (RMA)。写入进程内存，无外部副作用。"""
    if order_id not in _orders():
        return {"error": f"订单 {order_id} 不存在"}
    elig = check_return_eligibility(order_id)
    allowed = set(elig["summary"]["refundable"])
    target = [s for s in (skus or sorted(allowed)) if s in allowed]
    if not target:
        return {"error": "该订单没有可退商品，不能生成退货单",
                "eligibility_summary": elig["summary"]}
    # RMA 由 order_id + SKU 确定性派生 —— replay 多次得到同一个号，便于做 oracle。
    # 用 blake2b 而不是内置 hash()：str 的 hash 受 PYTHONHASHSEED 随机化影响，
    # 跨进程不稳定，会让 baseline 和 candidate 的 replay 结果无法比对。
    digest = hashlib.blake2b(("|".join(sorted(target))).encode("utf-8"), digest_size=4).hexdigest()
    rma = f"RMA-{order_id.split('-')[-1]}-{int(digest, 16) % 10000:04d}"
    _RETURN_LABELS[rma] = {"order_id": order_id, "skus": sorted(target), "status": "CREATED"}
    return {"rma_number": rma, "order_id": order_id, "skus": sorted(target),
            "status": "CREATED", "carrier": "SF Express",
            "instructions": "请在 7 天内将商品原包装寄回，并在包裹外标注 RMA 单号。"}
