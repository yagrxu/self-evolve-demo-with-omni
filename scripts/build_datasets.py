#!/usr/bin/env python3
"""生成三份 dataset：dev_local / prod_sim / verify。

为什么用脚本生成而不是手写 JSON
------------------------------
``expected_response`` 里的每一个金额、天数、折旧费都直接由 ``agent/tools.py``
的纯函数算出来 —— 也就是说 **oracle 和 agent 运行时用的是同一份算法**。

手写标注答案的话，一旦算错（优惠券分摊这种最容易错），整个评估就被污染了：
evaluator 会把正确回答判成错的，A/B 结论完全不可信。
``omni-self-evolution`` skill 对此有明确要求（Step 4）：

  > Construct oracles **independently from candidate generation**.
  > Allowed automatic evidence: deterministic tool results ...
  > **Never copy a failed production response into expected_response.**

这里满足的正是"deterministic tool results"这一条。

三份数据集的订单区间互不相交（见 agent/fixtures/orders.json 的 _id_ranges）——
``verify`` 是 Phase 6 的 held-out 集，用完全没见过的订单，
所以 Phase 6 的提升不可能来自"记住了答案"。

用法：
    python scripts/build_datasets.py            # 写 datasets/*.json
    python scripts/build_datasets.py --check    # 只校验, 不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

import tools as T  # noqa: E402

OUT_DIR = REPO / "datasets"

# 期望工具轨迹 —— **按工具层分开**。
#
# 为什么要分：agent 的工具集是分层的（见 agent/agent.py 的 BASIC_TOOLS /
# ENHANCED_EXTRA_TOOLS）。baseline 只有 basic 层的 4 个工具，
# 如果把 `check_return_eligibility` / `calculate_refund` 写进它的期望轨迹，
# TrajectoryInOrderMatch 会**因为工具根本不存在而 100% 失败** ——
# 那不是模型的问题，是评估设计的问题，会让 baseline 分数虚低、
# 进而让 Phase 5 的"提升"被夸大。
#
# 所以 `expected_trajectory` 存 basic 层的期望（baseline 与 c1/c3 用），
# enhanced 层的期望放进 metadata，供 Phase 5 的 candidate c2 使用。
TRAJ_BASIC = ["get_order", "get_refund_policy"]
TRAJ_ENHANCED_FULL = ["get_order", "get_refund_policy", "check_return_eligibility", "calculate_refund"]
TRAJ_ENHANCED_NO_MONEY = ["get_order", "get_refund_policy", "check_return_eligibility"]


# ── 场景规格表 ─────────────────────────────────────────────────────────────
# (order_id, archetype, 用户提问, 要退的商品 或 None=整单)
#
# 提问刻意写成真实用户的口吻：不提类目、不提政策、不给天数 ——
# agent 必须自己查工具才能答对。这是让"弱 prompt 一定会翻车"的关键设计。

DEV_SPECS = [
    ("ORD-10001", "refund_in_window",      "你好，我买的机械键盘想退掉，能退多少钱？", None),
    ("ORD-10002", "exchange_only",         "我这个耳机用着不太舒服，想退了，可以吗？", None),
    ("ORD-10003", "expired_warranty",      "我那台游戏本想退货，怎么操作？", None),
    ("ORD-10014", "apparel_full_return",   "这件羽绒外套颜色和图片不一样，我要退货，运费谁承担？", None),
    ("ORD-10005", "apparel_expired",       "运动短裤穿着不合身，我想退三条", None),
    ("ORD-10006", "books_in_window",       "买的编程书想退掉，能全额退吗？", None),
    ("ORD-10007", "home_in_window",        "空气炸锅我用了一次不太满意，想退，能退多少？", None),
    ("ORD-10008", "home_exchange",         "落地灯的灯罩我不喜欢，想退货", None),
    ("ORD-10009", "perishable_no_return",  "订的牛奶我不想要了，能退吗？", None),
    ("ORD-10010", "perishable_damaged",    "蓝莓收到的时候盒子压坏了，果子都烂了，怎么办？", None),
    ("ORD-10011", "coupon_single",         "智能手表想退，我当时用了 200 的券，能退多少？", None),
    ("ORD-10012", "coupon_partial_multi",  "我只想退一个手机壳，音箱留着，能退多少钱？", [{"sku": "PC-TPU", "qty": 1}]),
    ("ORD-10013", "damaged_out_of_window", "办公椅气压杆到货就是坏的，我一直没空处理，现在还能退吗？", None),
    ("ORD-10004", "multi_qty_partial",     "两件T恤我想退一件，另一件留着", [{"sku": "TS-CT01", "qty": 1}]),
    ("ORD-10015", "coupon_multi_full",     "显示器和硬盘我都想退，当时用了 150 的优惠券，一共能退多少？", None),
]

PROD_SPECS = [
    ("ORD-10101", "refund_in_window",      "无线鼠标昨天刚到，我想退掉，能退多少？", None),
    ("ORD-10102", "exchange_only",         "这个真无线耳机左边声音小，我想退货", None),
    ("ORD-10103", "expired_warranty",      "平板买了有一段时间了，想退，还来得及吗？", None),
    ("ORD-10104", "apparel_in_window",     "卫衣尺码偏大，我想退，需要我付运费吗？", None),
    ("ORD-10105", "apparel_expired",       "跑步鞋我想退了", None),
    ("ORD-10106", "books_in_window",       "算法这本书我想退，能退全款吗？", None),
    ("ORD-10107", "home_in_window",        "电饭煲想退掉，实际能退回多少钱？", None),
    ("ORD-10108", "home_exchange",         "羊毛地毯的花色和我家不搭，想退货", None),
    ("ORD-10109", "perishable_no_return",  "三文鱼我不要了，可以退款吗？", None),
    ("ORD-10110", "perishable_damaged",    "蛋糕礼盒送到的时候已经变形塌了，能退吗？", None),
    ("ORD-10111", "coupon_single",         "相机我想退，下单时用了 300 券，实际退我多少？", None),
    ("ORD-10112", "coupon_partial_multi",  "键盘留着，两个桌垫都退掉，退多少钱？", [{"sku": "MP-XL", "qty": 2}]),
    ("ORD-10113", "damaged_out_of_window", "沙发送来就有一道大口子，拖了挺久才来问，还能退吗？", None),
    ("ORD-10116", "multi_qty_partial",     "买了两支电动牙刷，退一支", [{"sku": "WD-A9", "qty": 1}]),
    ("ORD-10115", "mixed_category_full",   "投影仪和幕布我都要退，用了 250 的券，总共退多少？", None),
    ("ORD-10117", "books_expired",         "机器学习那本书我想退掉", None),
    ("ORD-10118", "home_exchange",         "咖啡机噪音太大了，我要退货", None),
    ("ORD-10119", "coupon_partial_multi",  "腰带我留着，袜子两袋都退了，能退多少？", [{"sku": "SK-CT03", "qty": 2}]),
    ("ORD-10120", "home_in_window",        "扫地机器人吸力不行，想退，能退多少钱？", None),
    ("ORD-10114", "apparel_full_return",   "真丝连衣裙有个线头开了，我要退，运费怎么算？", None),
]

VERIFY_SPECS = [
    ("ORD-10201", "refund_in_window",      "快充头我想退掉，能退回多少？", None),
    ("ORD-10202", "exchange_only",         "开放式耳机戴着容易掉，想退货可以吗？", None),
    ("ORD-10203", "expired_warranty",      "手机我想退，还能退吗？", None),
    ("ORD-10204", "apparel_in_window",     "西装长裤长度不合适，我想退，运费谁出？", None),
    ("ORD-10205", "apparel_expired",       "羊毛大衣我想退掉", None),
    ("ORD-10206", "books_in_window",       "面试指南这本书想退，能退全款吗？", None),
    ("ORD-10207", "home_in_window",        "加湿器有异味，我想退，实际退多少钱？", None),
    ("ORD-10208", "home_exchange",         "实木床架的颜色比图片深，想退货", None),
    ("ORD-10209", "perishable_no_return",  "草莓我不想要了，退款可以吗？", None),
    ("ORD-10210", "perishable_damaged",    "牛排礼盒收到时冰袋全化了，肉都变色了，怎么处理？", None),
    ("ORD-10211", "coupon_single",         "轻薄本想退，当时用了 400 的券，能退我多少？", None),
    ("ORD-10212", "coupon_partial_multi",  "鼠标留着，两根 HDMI 线都退，退多少？", [{"sku": "CB-HD2", "qty": 2}]),
    ("ORD-10213", "damaged_out_of_window", "橡木书桌桌面到货就有磕碰，我一直没处理，现在能退吗？", None),
    ("ORD-10216", "multi_qty_partial",     "两个充电宝退一个", [{"sku": "PB-20K", "qty": 1}]),
    ("ORD-10215", "mixed_category_full",   "电视和挂架都退，用了 350 券，总共退多少？", None),
    ("ORD-10217", "books_expired",         "云原生那本书我想退", None),
    ("ORD-10218", "home_exchange",         "蒸烤箱装不进我的橱柜，要退货", None),
    ("ORD-10219", "coupon_partial_multi",  "帽子留着，两袋T恤都退，能退多少？", [{"sku": "TT-CT09", "qty": 2}]),
    ("ORD-10220", "home_in_window",        "空气净化器噪音大，想退，能退多少钱？", None),
    ("ORD-10214", "apparel_full_return",   "短靴鞋跟有瑕疵，我要退，运费怎么算？", None),
]


# ── oracle 渲染 ────────────────────────────────────────────────────────────


def _m(x: float) -> str:
    return f"{x:.2f}"


def render_oracle(order_id: str, returns: list[dict] | None) -> tuple[str, list[str], list[str], list[str]]:
    """由工具的确定性输出渲染出 (expected_response, assertions, traj_basic, traj_enhanced)。

    渲染的答案覆盖所有"可核查的事实"：类目 / 窗口天数 / 距今天数 / 折旧费率与金额 /
    优惠券分摊 / 逐项退款 / 运费是否退 / 不可退时的替代方案。
    这样 Correctness 和自定义的 PolicyGrounding 都有明确的比对对象。
    """
    order = T.get_order(order_id)
    elig = T.check_return_eligibility(order_id)
    # get_order 刻意不返回 days_since_purchase（那是留给模型算的），
    # 但 oracle 需要真值，所以直接用纯函数。
    days = T.days_since_purchase(order_id)

    parts: list[str] = []
    asserts: list[str] = []

    # 每件商品的资格与政策依据
    for v in elig["items"]:
        pol = T.get_refund_policy(v["category"])
        cat_cn = pol["display_name"]
        if v["verdict"] == "ELIGIBLE_FULL_REFUND":
            parts.append(
                f"「{v['name']}」属于{cat_cn}类目，到货破损，依据 DAMAGED_OVERRIDE 条款"
                f"可全额退款：免收 {pol['restocking_fee_pct']}% 折旧费，原始运费与寄回运费均由商家承担"
                f"（下单已 {days} 天、已超出 {pol['return_window_days']} 天退货窗口也同样适用）。"
            )
            asserts += [
                f"必须说明「{v['name']}」因到货破损可全额退款，且免收折旧费",
                "必须说明破损情形下运费由商家承担",
                "不得因为已超出退货窗口而拒绝该破损商品的退款",
            ]
        elif v["verdict"] == "ELIGIBLE_REFUND":
            who = "商家" if pol["return_shipping_paid_by"] == "merchant" else "买家"
            parts.append(
                f"「{v['name']}」属于{cat_cn}类目，退货窗口 {pol['return_window_days']} 天，"
                f"您下单已 {days} 天，在窗口内可以退款；按政策收取 {pol['restocking_fee_pct']}% 折旧费，"
                f"寄回运费由{who}承担。"
            )
            asserts += [
                f"必须说明「{v['name']}」属于{cat_cn}类目，退货窗口为 {pol['return_window_days']} 天",
                f"必须说明距下单已 {days} 天，在退货窗口内",
                f"必须说明折旧费比例为 {pol['restocking_fee_pct']}%",
                f"必须说明寄回运费由{who}承担",
            ]
        elif v["verdict"] == "EXCHANGE_ONLY":
            parts.append(
                f"「{v['name']}」属于{cat_cn}类目，退货窗口 {pol['return_window_days']} 天，"
                f"您下单已 {days} 天，已超出退货窗口，**不能退款**；"
                f"但仍在 {pol['exchange_window_days']} 天换新窗口内，可以换同款新品。"
            )
            asserts += [
                f"必须明确告知不能退款，因为已超出 {pol['return_window_days']} 天退货窗口（当前 {days} 天）",
                f"必须主动提供换新方案，说明仍在 {pol['exchange_window_days']} 天换新窗口内",
                "不得承诺任何退款金额",
            ]
        else:  # NOT_ELIGIBLE
            if pol["return_window_days"] == 0:
                parts.append(f"「{v['name']}」属于{cat_cn}类目，一经售出不支持无理由退货；仅在到货破损或变质时可全额退款。")
                asserts += [
                    f"必须说明{cat_cn}不支持无理由退货",
                    "必须说明仅在破损或变质情形下才能退款",
                    "不得承诺任何退款金额",
                ]
            else:
                tail = (
                    f"，也超出了 {pol['exchange_window_days']} 天换新窗口"
                    if pol["exchange_after_window"] else ""
                )
                parts.append(
                    f"「{v['name']}」属于{cat_cn}类目，退货窗口 {pol['return_window_days']} 天，"
                    f"您下单已 {days} 天，已超出退货窗口{tail}，无法退款或换新，只能走厂商保修。"
                )
                asserts += [
                    f"必须明确告知无法退款，因为已超出 {pol['return_window_days']} 天退货窗口（当前 {days} 天）",
                    "必须给出保修作为唯一后续途径",
                    "不得承诺任何退款金额",
                ]

    # 有可退商品时给出精确金额
    refundable = elig["summary"]["refundable"]
    # basic 层无论可退与否，期望都只有"查订单 + 查政策"这两步 ——
    # 剩下的推理和算术在 basic 层本来就没有工具可用，必须由模型自己完成。
    traj_basic = list(TRAJ_BASIC)
    traj_enhanced = list(TRAJ_ENHANCED_FULL) if refundable else list(TRAJ_ENHANCED_NO_MONEY)

    if refundable:
        calc = T.calculate_refund(order_id, returns)
        if order.get("coupon") and calc["lines"]:
            coup = T.get_coupon_allocation(order_id)
            alloc_desc = "；".join(
                f"「{a['name']}」占原价 {a['share_pct']:.2f}%，分摊券额 {_m(a['coupon_allocated'])} 元，"
                f"分摊后实付 {_m(a['paid_after_coupon'])} 元"
                for a in coup["allocations"]
            )
            parts.append(
                f"优惠券 {order['coupon']['code']}（{_m(order['coupon']['amount'])} 元）按各商品原价比例分摊："
                f"{alloc_desc}。退款只退分摊后的实付金额，不退整张券面值。"
            )
            asserts.append(
                f"必须说明优惠券 {_m(order['coupon']['amount'])} 元按原价比例分摊，而不是整张券退还或直接忽略"
            )
            for a in coup["allocations"]:
                if a["sku"] in [l["sku"] for l in calc["lines"]]:
                    asserts.append(f"「{a['name']}」分摊到的券额必须为 {_m(a['coupon_allocated'])} 元")

        for line in calc["lines"]:
            parts.append(
                f"退款计算——「{line['name']}」退 {line['qty_returned']} 件：原价小计 {_m(line['list_subtotal_returned'])} 元，"
                f"分摊券额 {_m(line['coupon_allocated_returned'])} 元，分摊后实付 {_m(line['paid_after_coupon'])} 元，"
                f"折旧费 {line['restocking_fee_pct']:.0f}% 即 {_m(line['restocking_fee'])} 元，"
                f"该项可退 {_m(line['item_refund'])} 元。"
            )
            asserts.append(f"「{line['name']}」的退款金额必须为 {_m(line['item_refund'])} 元")
            if line["restocking_fee"] > 0:
                asserts.append(f"「{line['name']}」的折旧费必须为 {_m(line['restocking_fee'])} 元")

        parts.append(
            f"原始运费 {_m(calc['original_shipping_fee'])} 元："
            + ("退还 " + _m(calc["shipping_refund"]) + " 元。" if calc["shipping_refund"] > 0 else "不退还。")
            + calc["shipping_reason"]
        )
        parts.append(f"**合计可退 {_m(calc['total_refund'])} 元（人民币）。**")
        asserts += [
            f"合计退款金额必须为 {_m(calc['total_refund'])} 元",
            ("必须说明原始运费 " + _m(calc["original_shipping_fee"]) + " 元退还")
            if calc["shipping_refund"] > 0
            else ("必须说明原始运费 " + _m(calc["original_shipping_fee"]) + " 元不退还"),
        ]

        if returns:
            skus = ", ".join(r["sku"] for r in returns)
            asserts.append(f"只能计算用户指定退回的商品（{skus}），不得把整单都算进退款")

    for blocked in (T.calculate_refund(order_id, returns)["blocked"] if refundable else []):
        parts.append(f"（{blocked.get('sku')}：{blocked.get('reason', blocked.get('error', ''))}）")

    # 同类目多件商品会生成重复的政策 assertion（如"退货窗口为 14 天"）；
    # 去重但保持顺序，避免同一条检查被 evaluator 重复计权。
    deduped = list(dict.fromkeys(asserts))
    return "\n".join(parts), deduped, traj_basic, traj_enhanced


def build(specs: list[tuple], prefix: str) -> list[dict]:
    examples = []
    for i, (order_id, archetype, question, returns) in enumerate(specs, start=1):
        expected, asserts, traj_basic, traj_enhanced = render_oracle(order_id, returns)
        order = T.get_order(order_id)
        examples.append({
            "scenario_id": f"{prefix}_{i:03d}",
            "turns": [{"input": f"订单号 {order_id}。{question}", "expected_response": expected}],
            # basic 层（baseline / c1 / c3）的期望轨迹
            "expected_trajectory": traj_basic,
            "assertions": asserts,
            "metadata": {
                "archetype": archetype,
                "order_id": order_id,
                "days_since_purchase": T.days_since_purchase(order_id),
                "categories": sorted({it["category"] for it in order["items"]}),
                "has_coupon": bool(order.get("coupon")),
                "is_partial_return": returns is not None,
                "return_items": returns,
                # 追溯用：oracle 是谁算的、依据哪版政策
                # enhanced 层（Phase 5 candidate c2）的期望轨迹 —— 评估 c2 时用这个替换
                "expected_trajectory_enhanced": traj_enhanced,
                "oracle_evidence_type": "deterministic_tool_result",
                "oracle_source": "agent/tools.py::compute_refund + item_verdict",
                "policy_version": T.get_refund_policy(order["items"][0]["category"])["policy_version"],
                "as_of_date": order["today"],
            },
        })
    return examples


DATASETS = {
    # dataset 名必须匹配 [a-zA-Z][a-zA-Z0-9_]{0,47} —— 不能有连字符（manage_datasets 的约束）
    "dev_local": (DEV_SPECS, "dev", "Phase 2 本地基线数据集：15 条覆盖 15 类退换货原型"),
    "prod_sim": (PROD_SPECS, "prod", "Phase 3 云上流量模拟数据集：20 条，模拟真实用户请求"),
    "verify": (VERIFY_SPECS, "vfy", "Phase 6 held-out 验证数据集：20 条，订单与前两份完全不相交"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写盘")
    args = ap.parse_args()

    all_orders: dict[str, set[str]] = {}
    OUT_DIR.mkdir(exist_ok=True)
    problems: list[str] = []

    for name, (specs, prefix, desc) in DATASETS.items():
        examples = build(specs, prefix)
        all_orders[name] = {e["metadata"]["order_id"] for e in examples}

        for e in examples:
            if not e["turns"][0]["expected_response"].strip():
                problems.append(f"{e['scenario_id']}: expected_response 为空")
            if len(e["assertions"]) < 2:
                problems.append(f"{e['scenario_id']}: assertions 少于 2 条")

        payload = {
            "name": name,
            "description": desc,
            "dataSource": "local",
            "_generated_by": "scripts/build_datasets.py",
            "_oracle_note": "expected_response / assertions 全部由 agent/tools.py 的确定性纯函数渲染, "
                            "与 agent 运行时使用同一份算法, 因此标注答案不可能与真值不一致。",
            "examples": examples,
        }
        if not args.check:
            (OUT_DIR / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(f"{name:12s} {len(examples):3d} 条  订单 {len(all_orders[name])} 个  "
              f"assertions {sum(len(e['assertions']) for e in examples):3d} 条")

    # 关键不变量：verify 必须与前两份完全不相交，否则 Phase 6 的结论站不住
    overlap_pv = all_orders["prod_sim"] & all_orders["verify"]
    overlap_dv = all_orders["dev_local"] & all_orders["verify"]
    overlap_dp = all_orders["dev_local"] & all_orders["prod_sim"]
    for label, ov in (("prod_sim∩verify", overlap_pv), ("dev_local∩verify", overlap_dv),
                      ("dev_local∩prod_sim", overlap_dp)):
        if ov:
            problems.append(f"数据集订单重叠 {label}: {sorted(ov)}")
        else:
            print(f"✓ {label} = ∅")

    if problems:
        print("\n校验失败：")
        for p in problems:
            print("  -", p)
        return 1
    print("\n✓ 全部校验通过" + ("（--check 模式，未写盘）" if args.check else f"，已写入 {OUT_DIR}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
