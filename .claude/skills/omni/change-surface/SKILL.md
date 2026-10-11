---
name: omni-change-surface
description: Use when an agent misbehaves and you must decide WHAT to change — policy, tool, workflow, skill, prompt, or model. Reads trace and evaluator evidence, locates the lowest layer where the failure originates, and outputs one prioritized change surface with the evidence for it. Run this BEFORE omni-self-evolution, which only handles the prompt surface.
surfaces: [resources, install]
---

# Omni Studio: Change-Surface Triage

## Overview

When an agent gets something wrong, there are six places you could change. Changing the
wrong one is expensive in a specific way: the symptom moves, the evaluator score wobbles,
and you spend a replay budget learning nothing. This skill decides **which surface to
change first** and records why.

The output is a single decision: one surface, the evidence for it, and the surfaces you
ruled out. It does not make the change.

Use it when:
- You have at least one failing trace (low evaluator score, user complaint, or wrong answer).
- The agent runs end to end — no crash, no missing spans.

Do NOT use it for crashes, wiring bugs, or missing instrumentation. Those are code defects,
not change-surface decisions. Use `omni-diagnose-instrumentation-failure` or
`omni-trace-analysis` instead.

## The six surfaces

| Surface | What it is | Changes the agent's… | Blast radius | Re-baseline cost |
|---|---|---|---|---|
| **Policy** | Business source of truth the agent reads (rules, windows, fees, labels) | Facts | Every request that reads it, and every evaluator oracle built on it | High — the expected answers change |
| **Tool** | Tool code, return payload, name, description, parameter schema | Data and actions | Requests that call that tool | Medium |
| **Workflow** | Deterministic orchestration around the model: mandatory steps, ordering, branching, handoff | Control flow | Requests that take that path | Medium |
| **Skill** | A procedure loaded only for a subset of requests | Know-how for one task type | Only the matching task type | Low |
| **Prompt** | System instructions applied to every request | Behavior and judgment | Every request | Low per change, but changes are global |
| **Model** | The LLM itself | Capability ceiling, latency, cost | Everything | Highest — every baseline is invalid |

## The core rule

**Fix the lowest layer where the evidence pins the failure.** Walk the layers in this
order and stop at the first one that is actually wrong:

```
Policy  →  Tool  →  Workflow  →  Skill  →  Prompt  →  Model
(facts)   (data)   (control)    (know-how) (behavior) (capability)
```

The order is not "cheapest first". It follows information flow: each layer can only be as
correct as what it receives from the layer before it. A prompt that tells the model to
"double-check the refund math" cannot fix a tool that returned the wrong price, and a model
upgrade cannot fix a policy file that states the wrong window. Fixing upstream removes the
failure. Fixing downstream hides it, and it comes back on the next input the patch didn't
anticipate.

Two consequences:
1. **Prompt is not the default.** It is the fifth place to look. It is the right answer
   only once you have shown that facts, data, and control flow were all correct and the
   model still behaved badly.
2. **Model is last.** Change it only after the layers above have plateaued, because it
   invalidates every baseline you have.

## Hard Invariants

1. **One surface per change.** You MUST recommend exactly one surface, because a change
   that touches two surfaces makes the measured delta unattributable. If two surfaces are
   both broken, sequence them: upstream first, re-measure, then the next one.
2. **Evidence before verdict.** You MUST cite a trace ID or session ID and the specific span,
   tool payload, or evaluator explanation for each layer you clear or blame, because a
   verdict without evidence is a guess, and guesses default to "edit the prompt".
3. **Read-only.** You MUST NOT modify any surface. This skill produces a recommendation.

## Steps

### 1. Collect the failing evidence

For each failing trace, pull:
- The user input and final output.
- Every `execute_tool` span: the arguments and the full return payload.
- Every evaluator score with its explanation, and the trace ID.

You MUST read the tool return payloads, not just the span status. A tool span with status
OK can still return wrong or insufficient data.

### 2. Walk the layers

Answer each question with evidence. Stop at the first **yes**.

| # | Question | Typical signals | If yes → |
|---|---|---|---|
| 1 | Is the **source of truth itself** wrong, missing a rule, or ambiguous? | The agent quotes the policy faithfully and the answer is still wrong per the business. The evaluator oracle and the policy file disagree. Two readings of one field are both defensible. A field has no human-readable label, so the model invents one. | **Policy** |
| 2 | Did a tool return **wrong, incomplete, or hard-to-use** data, or was a tool picked or called wrong because of how it is described? | Tool payload contradicts the fixture. `ToolSelectionAccuracy` or `ToolParameterAccuracy` is low. The model does deterministic arithmetic, date math, or rule evaluation itself and gets it wrong. A distractor tool gets called. | **Tool** |
| 3 | Is a step **mandatory or order-dependent** and the model sometimes skips or reorders it? | "Must check X before Y" fails in some fraction of traces even with a clear instruction. Irreversible actions (create a label, issue a refund) happen before verification. A human-handoff condition is missed. | **Workflow** |
| 4 | Does the failure appear **only for one task type** that needs a long or conditional procedure? | Failures cluster in one archetype. The fix would add many lines that are irrelevant to most requests. Fixing it in the prompt regresses other archetypes. | **Skill** |
| 5 | Were facts, data, and flow all correct, and the model still **misjudged, contradicted itself, or communicated badly**? | Tool payloads are correct but the conclusion disagrees with them. The answer contradicts itself. Missing next step, wrong tone, wrong format. The failure is spread across archetypes. | **Prompt** |
| 6 | Have prompt, skill, and tool fixes **plateaued**, with the remaining failures being capability failures across many types? Or is the problem latency or cost? | Repeated well-targeted candidates fail to clear the gates. Failures need multi-step reasoning the model can't sustain. Latency or token cost is the complaint, not quality. | **Model** |

If no question gets a **yes**, the failure is likely judge noise or a bad evaluator. You
MUST say so rather than force a surface, and recommend checking evaluator agreement on that
trace.

### 3. Apply the tie-breakers

When the evidence supports two adjacent surfaces:
- **Deterministic beats probabilistic.** If the logic can be written as code (arithmetic,
  date windows, eligibility rules), put it in a **tool** or **workflow**, not in a prompt
  rule. A prompt rule lowers the error rate; code removes it.
- **Narrow beats global.** Between skill and prompt, choose the **skill** when the fix only
  serves one task type, because every prompt line is paid for, and can regress, on every
  request.
- **Source beats patch.** If the prompt would need to correct or relabel something a tool or
  policy returns, fix the **tool or policy** output instead.
- **Reversible beats irreversible.** If two fixes are equally well supported, recommend the
  one with the smaller blast radius and cheaper re-baseline first.

### 4. Write the decision

Output exactly this structure:

```
Surface: <Policy | Tool | Workflow | Skill | Prompt | Model | None (evaluator noise)>
Evidence: <trace/session IDs + the span, payload, or evaluator explanation that proves it>
Ruled out (upstream layers checked and cleared):
  - Policy: <why it is correct>
  - Tool: <why it is correct>
  - …
Next surface to re-check after this fix: <surface or "none">
Hand-off: <which skill or process makes the change, e.g. omni-self-evolution for Prompt>
```

The "Ruled out" list is the point of this skill. A decision without it means the upstream
layers were never checked.

## Worked examples from this demo

| Case | Evidence | Surface | Why not the others |
|---|---|---|---|
| `dev_002`, headphones, exchange-only (`selfevolve-dev_local-dev_002-30130a4e`) | `get_order` and `get_refund_policy` both return correct data. Policy says 23 days is outside the 14-day refund window and inside the 30-day exchange window. Output still says "✅ 可以退货" and quotes a refund. GoalSuccessRate 0, Faithfulness 0.25. | **Prompt** | Policy is correct and was read. Tool payloads are correct and ToolSelection/ToolParameter are 1.0. The flow called the right tools in the right order. The failure is the model's judgment over correct data. That is why c5 changes only the system prompt. |
| Refund arithmetic, coupon allocation (`low_Correctness`, `coupon_multi_full`) | The basic-tier `get_order` returns raw prices and no allocation. The model splits the coupon itself and gets the rounding wrong. | **Tool** | Candidate `c3-arithmetic-rules` tried to teach the arithmetic in the prompt. Under the deterministic-beats-probabilistic rule this belongs in code: `get_coupon_allocation` / `calculate_refund` already exist in the enhanced tier. |
| `restocking_fee_pct` called "折旧费" | The policy field has no human-readable label, so the model invents a name. | **Policy** (or the tool payload) | Prompt rule 5 in c1/c5 patches it, but source-beats-patch says to add the label to the policy data, so every consumer, including the evaluator oracle, gets the same term. |
| `prod_014` / `prod_017` | The second call has ToolSelectionAccuracy or ToolParameterAccuracy 0.0. | **Tool** (description or schema) | Check first whether the description of `search_knowledge_base`, the deliberate distractor, makes it look like a policy source. A prompt rule saying "don't use that tool" is a downstream patch. |
| Quoting a refund before checking eligibility | An ordering requirement that must hold 100% of the time. | **Workflow**, if it still fails after a prompt rule | Prompt rules 1–2 reduce this. If any fraction of traces still skips the check before an irreversible step, move it into orchestration. |

## Anti-patterns

- **"Just tweak the prompt."** The most common wrong answer, because it is the easiest edit.
  Only valid after step 2 cleared policy, tool, and workflow with evidence.
- **Upgrading the model to escape diagnosis.** It may move the score while leaving the root
  cause in place. It also invalidates every baseline, so you cannot tell whether it helped.
- **Changing two surfaces at once** ("new prompt plus a new tool"). If the score moves you
  cannot say which change caused it.
- **Trusting span status.** OK means the tool didn't throw an error. It does not mean it
  returned the right thing.
