# p1 —— Prompt 层（人工撰写）

**依据（control × 4 批次，Correctness 带标准答案）**：系统性失败（≥3/4）5 个 scenario：
- prod_005 apparel_expired、prod_008 home_exchange：超窗口仍暗示可退 / 前后矛盾 —— insight 根因 "Reasoning-Action Contradiction"（6 session）、"Internal Consistency Failures"（3）、"Window Boundary"（3）
- prod_013 damaged_out_of_window、prod_020 apparel_full_return：结论对但不写具体金额 / 运费
- prod_014 multi_qty_partial：先开退货单再查政策（Workflow 层，本轮不处理）

**机制**：在原 prompt 后追加两句：①单一结论、超窗口不暗示例外；②可退时写出逐件金额、原始运费与合计。
不教任何计算方法（算术错误 1/4 偶发，属 Tool 层，本轮不碰）；不提工具名（不越层到 Workflow）。
