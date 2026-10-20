---
name: omni-self-evolution-v2
description: DRAFT for review. Use when improving a deployed agent with evidence from production traces using CloudWatch Omni. Snapshots every layer, checks the production ruler, diagnoses failures by layer with repeated sampling, calibrates and pre-registers staged gates, changes exactly one layer per round, and confirms on never-seen data. Stops for human confirmation after every step. Never deploys.
---

# Omni Studio: Self-Evolution (v2 draft)

> 草案：吸收了一次真实运行的 18 条修订意见（`.omni/self-evolution/strands_agents-20261019T125024Z/skill-notes.md`）。
> 与 v1 的主要差别见文末。执行脚本：`training/omni/scripts/run_step.sh`。

## Operating rule
Run one step, then **stop**. Report: what was done (exact tools/commands), why the step exists,
results as counts, what to screenshot (terminal + Omni panel + AWS console), artifact paths.
Wait for the user before continuing.

## Hard invariants
1. Production is read-only. No deploy, publish, or remote push.
2. **One layer per round.** The baseline snapshot records every layer; the mutable layer is chosen after diagnosis.
   All other layers must stay byte-identical to the snapshot (checked at Step 11).
3. **The ruler is fixed before the run.** Step 4 checks it; it does not choose it. The local replay ruler must match
   production where possible; differences are recorded.
4. **Gates are calibrated (A/A) and registered before any candidate result exists.** Later changes are allowed only with
   a recorded reason and `calibrated_after_results: true`.
5. The split is a pure function of `scenario_id`; diagnosis and candidate design read **control only**.
6. Artifacts stay under `.omni/self-evolution/<run_id>/` (git-ignored).

## Steps
1. **Preflight** — credentials, prompt managed by `prompts.json`, override hook, span version **and hash**, ping.
2. **Snapshot all layers** — policy, data, tool (tier, descriptions, implementation), workflow, skill, prompt, model.
2.5 **Confirm production traffic exists** in the window; if this is a demo, generate it and record dataset, endpoint, time window.
3. **Harvest traces (read-only)** — `discover_agent_traces_metadata` first; use map access (`attributes['session.id']`) and the real agent column.
4. **Check production evaluation results** — coverage, observed values ⊂ declared scale (`get_evaluator`), failing counts,
   gaps (safety present? reference-free evaluators? evaluators the local path cannot run). Freeze the replay ruler.
   Any judge proposed as a hard gate must pass a self-consistency and false-positive test.
5. **Split** — holdout by stable hash **and** a minimum count (n ≈ (1.96σ/margin)²); seal a never-seen confirmation set.
6. **Diagnose** — baseline unchanged, ≥ 3 repetitions **in independent batches**; per-scenario k/B; attribute each failure to a
   layer from tool payloads and judge text (table in `training/cloudwatch/workflow.md §2`). A model verdict requires an explicit
   instruction that is violated across archetypes. The user chooses the layer.
6b. **Calibrate and register gates** — A/A between two baseline batch sets; compute thresholds from noise, flip resolution, and
   business budgets; warn if a gate is unreachable at this sample size; register per stage.
7. **Candidates for the chosen layer only** — full standalone artifacts; no answers or IDs; one additive and one restructure
   variant when the layer is prompt; check whether the candidate will expose a downstream-layer defect to users.
8. **Paired replay** — same period, same order; verify version on every cell; record latency and tokens; if the environment
   changes mid-run (IDE extension, MCP auth), rerun all arms.
9. **Gates (S0)** — script only; judge scored ≥ 3 times per cell (or report judge re-score Δ); safety uses only validated judges.
10. **Confirm (S1)** — register S1 first; one candidate; run once; no replacement of results.
11. **Decide** — tamper check, other-layers-unchanged check, `decision.json` with every gate, calibration record, and scope
    ("non-inferior locally" ≠ "better"). Local write-back only; S2 cloud canary is a separate, human-approved step.

## Outcomes
`WINNER` (registered gates passed) · `WINNER_UNDER_CALIBRATED_GATES` (passed after a recorded calibration) ·
`NO_CHANGE` (measured, did not pass) · `NO_DECISION` (no valid measurement).

## What changed from v1
- Baseline is a snapshot of all layers, not a lock on "prompt only".
- Step 4 checks the production ruler instead of choosing evaluators mid-run.
- New diagnosis step with repeated, independent batches and per-layer attribution.
- Gates are dynamic: A/A-calibrated, reachability-checked, pre-registered, and staged S0 → S3.
- Holdout has a minimum size; a never-seen confirmation set is sealed at split time.
- Judges: real scale from `get_evaluator`; multi-scoring; false-positive testing before using as a hard gate.
