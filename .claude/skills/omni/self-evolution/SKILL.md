---
name: omni-self-evolution
description: Use when improving a deployed agent's prompt using evidence from production traces. Harvests failing traces read-only, proposes prompt-only candidates, runs paired local replay with frozen evaluators, and applies statistical gates to reach one of four auditable decisions. Does not deploy, publish, or modify any production resource.
surfaces: [resources, install]
---

# Omni Studio: Self-Evolution

## Overview

This is a **procedural script**, not a set of suggestions. You MUST execute the steps in
order and validate each one before advancing.

It takes a deployed agent whose quality is unsatisfactory and produces an **auditable
decision** about a prompt change: harvest production traces read-only, score them with
frozen evaluators, generate prompt-only candidates, replay them locally against the
baseline in a paired design, apply statistical gates, and confirm on a held-out split.

Use it when all of these hold:
- The agent is deployed and emitting traces to Omni.
- Its prompts are already under Omni prompt management (run `omni-prompt-setup` first).
- You have concrete quality complaints, or evaluator scores below target.

Do NOT use it to fix a crash, a wiring bug, or missing instrumentation — a prompt change
cannot repair those, and this SOP will burn a full replay budget before telling you so.
Use `omni-diagnose-instrumentation-failure` or `omni-trace-analysis` instead.

### What this SOP is fighting

Prompt tuning fails in three specific ways, and every gate below exists to block one:

| Failure | How it happens | Blocked by |
|---|---|---|
| **Fooling yourself** | Eyeball two outputs, prefer the prettier one, ship it | Frozen evaluators + paired replay (Steps 4, 8) |
| **Winning on noise** | LLMs are nondeterministic; a 2% "gain" is a coin flip | Repetitions + paired bootstrap CI (Steps 8, 9) |
| **Overfitting the evidence** | Tune until the traces you looked at pass | Stable held-out split, hashed before candidates exist (Steps 6, 10) |

## Hard Invariants

These hold for the entire run. You MUST NOT relax them because a run that violates any of
them produces a conclusion that cannot be trusted, which is worse than no conclusion. You
MUST NOT ask the user for permission to relax them, since consent does not make an
unattributable measurement attributable.

1. **Production is read-only.** You MUST NOT write to a deployed resource, because this
   SOP runs against live customer traffic and a mistaken write is a production incident.
   Trace and log access is read-only; modify, delete, and reconfigure are all forbidden.
2. **Only the prompt may change.** Model ID, tool set, agent code, evaluator definitions,
   and dataset contents MUST be byte-identical between baseline and every candidate,
   because any co-varying change makes the measured delta unattributable.
3. **No deployment, no publishing.** You MUST NOT ship anything, because this SOP produces
   a *recommendation* — promoting it is a human decision with blast radius this SOP cannot
   assess. That covers deploying, publishing a prompt, changing a canary, writing to SSM,
   and pushing to a remote.
4. **Evaluators are frozen.** You MUST NOT touch an evaluator after Step 4, because
   changing the ruler mid-experiment lets you manufacture any result you want. That covers
   adding, removing, rewording, and re-scaling.
5. **The split precedes the candidates.** You MUST NOT re-split later, because re-rolling
   the split until a candidate passes is p-hacking, and it is undetectable in the final
   report.
6. **Artifacts stay out of source control.** You MUST write all artifacts under
   `.omni/self-evolution/<run_id>/` and confirm that path is git-ignored, because
   harvested traces can contain customer payloads.

## Parameters

- **agent_name** (required): The agent's name as it appears in trace attribute
  `attributes.agent.name` (for example `order_support_agent`). Used to filter traces.
- **prompt_name** (required): The key in `prompts.json` whose content will be varied
  (for example `order_support`). This is the single mutable axis.
- **quality_complaint** (required): What is wrong, in plain language, with at least one
  concrete example (for example "quotes refund amounts for items that are outside the
  return window; see order ORD-10008"). Drives candidate hypotheses in Step 7.
- **trace_window_hours** (optional, default: 24): How far back to harvest traces. Widen
  only if Step 3 yields too few eligible examples.
- **candidate_count** (optional, default: 3): Number of prompt candidates to replay.
  Values above 4 multiply cost without improving the decision.
- **evaluators** (optional): Explicit evaluator ID list. When omitted, Step 4 selects
  them and records the selection.

**Constraints for parameter acquisition:**
- If all required parameters are already provided, You MUST proceed to the Steps
- If any required parameters are missing, You MUST ask for them before proceeding
- When asking for parameters, You MUST request all parameters in a single prompt
- When asking for parameters, You MUST use the exact parameter names as defined
- You MUST NOT infer `quality_complaint` from evaluator scores alone, because a score
  tells you a dimension is low but not which behavior to change, and a candidate written
  against a guessed complaint usually regresses something else

## Steps

### 1. Preflight the prerequisites

Confirm the environment can actually support a defensible experiment, before spending
any replay budget.

You MUST verify, and record each result:
1. `check_credentials` returns `can_sign: true`. If `ready` is false with
   `auth_status: "transient"`, You MAY proceed for local work but MUST re-check before
   Step 2, because cloud trace queries will fail without a configured endpoint.
2. `prompts.json` exists and contains `prompt_name`, and the loader reads the model
   config from it. If the model is hardcoded in agent code, You MUST stop — the "prompt
   only" invariant cannot be enforced when `prompts.json` is decorative.
3. The prompt loader supports whole-file override via the `OMNI_PROMPTS_OVERRIDE`
   environment variable. This is the isolation mechanism for candidates; without it,
   variants differ by edits to a shared file and cross-contamination is undetectable.
4. Spans carry `llm.prompt_template.version` and `llm.prompt_template.hash`. Invoke the
   agent once and confirm. Without version attribution on traces, replay results cannot
   be tied back to a variant.

**Constraints:**
- You MUST use `manage_test_agent` with `action: "ping"` to confirm the agent responds
  before continuing. Do NOT skip to trace harvesting on the assumption it is running.
- If prompt attribution is missing from spans, You MUST check whether the agent was
  started under an OpenTelemetry auto-instrumentation wrapper, because a bare interpreter
  start leaves a `ProxyTracerProvider` in place that silently accepts no span processor.
- If any of items 2–4 fails, You MUST hard stop and report which prerequisite is missing.
  Do NOT continue with a workaround, since every downstream number would be
  unattributable and the run would have to be discarded anyway.

### 2. Open the run and freeze the baseline

Create the run directory and record exactly what "before" means, so the comparison
cannot drift.

You MUST create `.omni/self-evolution/<run_id>/` where `run_id` is
`<agent_name>-<UTC timestamp>`, and write `manifest.json` containing: `agent_name`,
`prompt_name`, `quality_complaint`, all resolved parameter values, the baseline prompt
version, the baseline prompt content hash, the model ID, the sorted tool-name list, the
agent code commit SHA, and the UTC start time.

**Constraints:**
- You MUST compute the prompt hash with a hash function that is stable across processes.
  Do NOT use a language's built-in string hash, because several randomize per process and
  the Step 11 tamper check would fail spuriously.
- You MUST confirm `.omni/` is git-ignored before writing anything into it.
- You MUST record the tool list and model ID even though they are frozen, because they are
  what makes the freeze auditable after the fact.

### 3. Harvest production traces (read-only)

Pull the agent's recent traces into the run directory.

You MUST use `search_agent_traces` and You MUST write raw results to
`<run_dir>/traces/raw.jsonl`. Query in a subagent, or have the subagent write the file
directly, because raw traces are large and will crowd out the orchestrator's context.

**Constraints:**
- You MUST bind both ends of the time range with integer-second timestamps. An open,
  missing, or over-wide upper bound is rejected outright rather than clamped, wasting the
  round trip.
- You MUST include `AND "@telemetry_type" = 'traces'` and You MUST parenthesize your own
  predicates, because `AND` binds tighter than `OR` and an unparenthesized filter leaks
  log records into the result set.
- You MUST filter on `attributes.agent.name`. Do NOT filter on `service.name`, since that
  column does not exist and the query will return zero rows that look like "no traffic".
- You MUST call `discover_agent_traces_metadata` before writing your first query, to get
  the real column names for this data store.
- You MUST treat a zero-row result as "wrong query or wrong region" until proven
  otherwise, because that is far more common than genuinely absent traffic. Verify the
  region matches where the agent is deployed.
- If the result set is empty after widening `trace_window_hours` once, You MUST stop with
  `NO_DECISION` and report the query you ran.

### 4. Freeze the evaluator set

Decide how quality will be measured, and lock it.

You MUST call `manage_evaluations` with `action: "list_evaluators"`, select the set, and
write it to `<run_dir>/evaluators.json` with, for each evaluator: its ID, its level
(TRACE / TOOL_CALL / SESSION), and its scoring semantics — direction, minimum, maximum,
pass threshold, and the canonicalization formula that maps its raw score to [0, 1].

**Constraints:**
- You MUST include at least one evaluator per axis named in `quality_complaint`. A
  complaint about invented policy numbers is not measured by a generic correctness score.
- You MUST include the safety evaluators (harmfulness, PII, and any security evaluator
  available), because they are hard gates in Step 9 and cannot be added later.
- You MUST record scoring semantics explicitly, even for built-in evaluators. Different
  evaluators use different scales and directions; aggregating them without a recorded
  canonicalization silently weights them by scale.
- You MUST NOT modify `<run_dir>/evaluators.json` after this step for any reason, because
  Invariant 4 is what makes the final delta meaningful.
- You SHOULD prefer a cheaper judge model for the evaluators, because judge invocations
  outnumber agent invocations by roughly the evaluator count times the repetition count.

### 5. Score the baseline and locate the failure modes

Establish where the agent actually stands, and on which axes.

You MUST run the frozen evaluators over the harvested traces via `manage_evaluations`
with `action: "run"`, and write per-trace scores to `<run_dir>/baseline_scores.json`.

**Constraints:**
- You MUST use `action: "run"` to produce scores. Do NOT use `action: "results"` for this,
  because it only reads evaluation spans that already exist and will report an empty or
  stale picture as if it were the baseline.
- You MUST group the low-scoring traces into named failure modes and write them to
  `<run_dir>/failure_modes.json`, each with: a name, the evaluator dimensions it depresses,
  the count of affected traces, and up to three example trace IDs.
- You SHOULD use `manage_annotations` to mark the traces you classified, so a human can
  audit the classification later.
- You MUST NOT write a summary table of scores into the conversation, because reproducing
  score tables in context invites transcription drift and consumes budget needed for later
  steps. Report only the counts per failure mode.
- If no failure mode accounts for at least two traces, You MUST stop with `NO_DECISION`:
  a single-trace complaint cannot be distinguished from noise by any downstream gate.

### 6. Build the eligible set and split it

Turn traces into replayable examples, then split them — before any candidate exists.

You MUST write `<run_dir>/datasets/eligible.json` containing only examples that carry
everything replay needs: the user input, a stable `scenario_id`, and the baseline output.
Then You MUST split it into `control.json` and `holdout.json`.

**Constraints:**
- You MUST assign each example to holdout when a stable hash of its `scenario_id` falls in
  the lowest 30% of the hash space. The split MUST be a pure function of `scenario_id` so
  that re-running this SOP produces the identical split; a random or run-seeded split can
  be re-rolled until a candidate passes, and nothing in the report would reveal it.
- You MUST require at least **12** eligible examples. Below 12, a 30% holdout yields
  fewer than 4 examples, where one example moves the mean by more than 25% — the gates in
  Step 9 would be reporting arithmetic on noise.
- You MUST require at least **5** control and **5** holdout examples after splitting. An
  imbalanced split can satisfy the total while leaving one side unusable.
- If any minimum is unmet, You MUST stop with `NO_DECISION` and report the actual counts.
  You MUST NOT lower a threshold or synthesize examples, because each of those converts an
  honest "insufficient evidence" into a false positive.
- You MUST record each example's expected tool trajectory for the **baseline's** tool
  tier. If the agent exposes tiered tool sets, an expectation written against a tier the
  baseline does not have makes every trajectory evaluator fail for a reason unrelated to
  the prompt, systematically inflating any candidate's apparent gain.

### 7. Generate prompt-only candidates

Write `candidate_count` prompt variants, each targeting a failure mode from Step 5.

For each candidate You MUST write a complete standalone prompts file at
`<run_dir>/candidates/<candidate_id>/prompts.json`, and a `rationale.md` naming the
failure mode it targets and the mechanism by which it should help.

**Constraints:**
- You MUST make each candidate a full copy of the baseline file with only the
  `prompt_name` content changed. Whole-file isolation via `OMNI_PROMPTS_OVERRIDE` makes
  "nothing else changed" a structural property rather than a matter of discipline.
- You MUST keep `model` and every other key byte-identical to the baseline, because
  Invariant 2 is the only reason the measured delta can be attributed to the prompt.
- You MUST give each candidate a distinct prompt `version` string, because that string is
  what lands on spans and is how replay results are attributed to variants.
- Each candidate SHOULD target exactly one failure mode. A candidate that changes four
  things at once tells you the bundle helped but not which part, so it cannot be
  reduced to a minimal safe change.
- You SHOULD include one candidate that only *adds* explicit constraints, and one that
  restructures the instructions, because these fail in different ways and comparing them
  is informative even when both lose.
- You MUST NOT put expected answers into a prompt, because that memorizes the eligible set
  and will not generalize to the holdout. This includes specific record IDs and values
  copied from the examples.

### 8. Paired replay on the control set

Measure each candidate against the baseline on identical inputs.

For the baseline and every candidate, You MUST replay all control examples
`replay_repetitions` times (default **3**), and write raw per-invocation results to
`<run_dir>/runs/<variant_id>/`. You MUST invoke through `manage_test_agent` with
`action: "invoke_agent"` against a locally started agent.

**Constraints:**
- You MUST replay the baseline in this step too, in the same session and under the same
  conditions. Do NOT reuse the Step 5 production scores as the baseline arm, because those
  came from different traffic under a different model-endpoint state, so the comparison
  would not be paired and the bootstrap in Step 9 would be invalid.
- You MUST replay all variants over the same example list in the same order, because the
  paired design is what removes per-example difficulty from the comparison.
- You MUST select each variant solely by setting `OMNI_PROMPTS_OVERRIDE` to that
  variant's prompts file. Do NOT edit a shared prompts file between arms, since a stale
  in-memory copy or a missed revert silently mislabels an entire arm.
- You MUST verify, from each response or its trace, that the prompt version actually in
  effect matches the intended variant. You MUST discard and re-run any invocation where it
  does not match, because a single mislabeled invocation biases that arm's mean.
- You MUST use at least 3 repetitions, because LLM sampling is nondeterministic and a
  single pass provides no within-example variance estimate — with one pass you cannot tell
  a real gain from resampling the same prompt.
- You MUST pin any date- or time-dependent behavior to a fixed value for the whole run.
  Otherwise the same example yields different correct answers across arms, and the
  difference is attributed to the prompt.
- You MUST record latency and token counts per invocation, because they are gates in
  Step 9 and cannot be reconstructed afterward.
- You MUST run this step via a script rather than by issuing invocations yourself, since
  the loop is deterministic bookkeeping over variants, examples, and repetitions, and
  hand-driving it drops or duplicates cells.

### 9. Apply the gates

Compute the verdict for each candidate. This step is arithmetic; it MUST be a script.

You MUST write `<run_dir>/gates.json` recording, per candidate, every gate's computed
value, its threshold, and pass or fail.

A candidate passes only if **all** of these hold:

| Gate | Threshold | Why this threshold |
|---|---|---|
| Mean quality delta | ≥ **+0.03** canonicalized | Below this, the difference is within observed evaluator run-to-run spread — indistinguishable from re-scoring the same output |
| Paired bootstrap CI | **95%**, lower bound **≥ 0** | Resamples examples, not invocations, so the interval reflects generalization to new inputs rather than to more samples of the same ones |
| Per-evaluator regression | ≤ **0.02** on every evaluator | Blocks a candidate that buys its average by trading away one dimension — usually the one a user will notice |
| Latency regression | ≤ **10%** | A prompt that wins by reasoning far longer has a cost the quality score does not capture |
| Token regression | ≤ **10%** | Same reasoning, on the axis that shows up on the bill |
| Safety | **Zero** new failures | Hard gate: safety is not tradeable against quality at any delta |

**Constraints:**
- You MUST implement the gate computation as a script and You MUST NOT compute deltas,
  intervals, or pass/fail judgments in your own output, because these have exact correct
  answers and LLM arithmetic on multi-arm score tables is unreliable.
- You MUST bootstrap over examples, not over invocations, because resampling invocations
  estimates how well you measured *these* examples, which is not the question.
- You MUST treat a missing gate input as a failure, not as a pass. A candidate with no
  recorded latency has not demonstrated it did not regress latency.
- You MUST NOT adjust any threshold after seeing the results, because a threshold chosen
  to admit the candidate in front of you is not a gate.
- If no candidate passes, You MUST proceed to Step 11 with `NO_CHANGE`. This is a
  successful run: it establishes that these hypotheses do not beat the baseline.

### 10. Confirm the winner on the holdout

Verify the leading candidate on examples it was never tuned against.

You MUST take the highest-delta passing candidate, replay it and the baseline on
`holdout.json` with the same repetition count, and re-apply every Step 9 gate. Write
results to `<run_dir>/holdout/`.

**Constraints:**
- You MUST evaluate exactly one candidate here. Testing several against the holdout
  spends its independence on candidate selection, which is what the control set is for.
- You MUST apply the identical gates with identical thresholds, because a holdout checked
  more leniently than the control set confirms nothing.
- If the winner fails any gate on the holdout, You MUST record `NO_CHANGE` and You MUST
  report that it passed on control but failed on holdout — that specific pattern is
  evidence of overfitting and is the most valuable finding this SOP can produce.
- You MUST NOT promote the runner-up after the leader fails the holdout, because the
  holdout has now been observed and a second candidate judged against it is no longer
  held out.

### 11. Record the decision and write back locally

Produce the auditable output. This is the only step that may modify a file outside the
run directory.

You MUST write `<run_dir>/decision.json` and `<run_dir>/report.md`, with the outcome as
exactly one of:

| Outcome | Meaning |
|---|---|
| `WINNER` | A candidate passed every gate on both control and holdout |
| `NO_CHANGE` | Candidates ran and were measured; none passed. The baseline stands |
| `NO_DECISION` | The run could not produce a valid measurement (insufficient examples, no traces, failed preflight) |
| `ROLLED_BACK_LOCAL` | A local write-back was applied and then reverted |

**Constraints:**
- On `WINNER`, You MUST re-hash the baseline prompt file and compare it to the hash in
  `manifest.json` before writing. If it differs, the file changed mid-run, so the
  experiment measured something other than the current file and You MUST record
  `NO_DECISION` instead of writing.
- On `WINNER`, You MAY apply the winning content to the local `prompts.json` and MUST
  record the pre-write content so the change is revertible in one step.
- You MUST NOT ship the winner anywhere, because promotion needs human review of blast
  radius that this SOP has no way to evaluate. Per Invariant 3, that covers publishing the
  prompt, deploying, changing a canary, writing to SSM, and pushing to a remote.
- `decision.json` MUST include the run ID, the outcome, every gate value with its
  threshold, the example counts for both splits, the frozen evaluator list, and the
  baseline and winner prompt hashes. A decision that cannot be recomputed from its own
  record is not auditable.
- `report.md` MUST state the outcome in its first line, and MUST report per-evaluator
  deltas rather than only the aggregate, because the aggregate hides exactly the
  single-dimension regression that Step 9's per-evaluator gate exists to catch.
- You MUST distinguish `NO_CHANGE` from `NO_DECISION` in your summary to the user.
  `NO_CHANGE` is a measurement; `NO_DECISION` means no measurement was possible, and
  conflating them lets an unmeasured run be read as a validated baseline.
- You SHOULD state the single highest-value next experiment, based on which failure mode
  from Step 5 remains unaddressed.

## After This SOP

Cloud verification is deliberately out of scope. Promoting a `WINNER` means deploying it
and re-measuring on traffic it has never seen — a separate, human-approved action. When
you do, You SHOULD verify against a third example set that is disjoint from both
`control.json` and `holdout.json`, because both have now been observed, and reusing them
measures memorization rather than improvement.

## Examples

### Example 1: A winner

**Input:**
- agent_name: `order_support_agent`
- prompt_name: `order_support`
- quality_complaint: "States a refund amount for items outside the return window; see ORD-10008"

**Expected behavior:** Harvests 24h of traces, freezes 9 evaluators, finds 3 failure modes,
builds 18 eligible examples (12 control / 6 holdout), replays 3 candidates × 3 repetitions,
one candidate clears every gate on control and holdout. Outcome `WINNER`, written back
locally, not deployed.

### Example 2: Overfitting caught by the holdout

**Input:** as above, with candidate_count: 4

**Expected behavior:** A candidate gains +0.11 on control and clears every gate, then
regresses on holdout. Outcome `NO_CHANGE`, with the control-pass/holdout-fail pattern
called out explicitly as overfitting evidence. The runner-up is NOT promoted.

### Example 3: Insufficient evidence

**Input:** as above, with trace_window_hours: 1

**Expected behavior:** Harvest yields 7 eligible examples, below the minimum of 12. The
window widens once, still short. Outcome `NO_DECISION` with actual counts reported. No
threshold is lowered and no examples are synthesized.

## Troubleshooting

### Trace query returns zero rows

Check, in this order: the region matches where the agent is deployed; both ends of the
time range are bound with integer seconds; `"@telemetry_type" = 'traces'` is present and
your own predicates are parenthesized; you filtered on `attributes.agent.name` and not
`service.name`; the column names came from `discover_agent_traces_metadata` rather than
from memory. Zero rows most often means the column does not exist or the region is wrong,
not that there is no traffic.

### Spans carry no prompt version

The agent was probably started without the OpenTelemetry auto-instrumentation wrapper, so
`trace.get_tracer_provider()` returned a proxy provider that accepts no span processor.
Start the agent under the wrapper, matching the deployed entry point. Re-register the
processor lazily on each invocation as well, because a provider installed later replaces
the proxy and a registration attempted only at import time is silently lost.

### Candidate scores are identical to baseline

`OMNI_PROMPTS_OVERRIDE` is likely not taking effect. Confirm the loader reads it on every
call rather than caching at import, and check the prompt version echoed by the response
against the intended variant.

### Every trajectory evaluator fails on all arms

The expected trajectory was almost certainly written against a different tool tier than
the one the agent is running. Fix the expectation to match the baseline's tier and re-run
from Step 8 — do NOT interpret this as a prompt problem, since it inflates every
candidate's apparent gain equally.

### `check_credentials` reports `ready: false`

If `auth_status` is `transient`, local tools still work; treat it as a warning. If cloud
calls fail with "Cloud endpoint not configured", the host process serving the MCP server
lacks a region in its environment — restart it with the region set. A stdio-to-TCP proxy
does not forward environment variables to the server.
