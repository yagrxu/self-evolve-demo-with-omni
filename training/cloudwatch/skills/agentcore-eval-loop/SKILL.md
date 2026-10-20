---
name: agentcore-eval-loop
description: Use when improving an AgentCore-hosted agent with evaluation evidence using only AWS CLI — deploy with CDK, verify CloudWatch GenAI Observability, register an AgentCore dataset, run ground-truth batch evaluations, classify failures with insights, generate a single-layer candidate, and decide with pre-registered, staged gates. Stops for human confirmation after every step. Never promotes to production without explicit approval.
---

# AgentCore Evaluation Loop (CLI-only)

## Overview
A procedural script. Execute the steps in order; after **every** step, stop and report to the user:
what was done (exact commands), why the step exists, the results as counts, what to screenshot,
and the artifact paths. Do not start the next step until the user confirms.

All commands live in `training/cloudwatch/scripts/`. Configuration is `training/cloudwatch/config.env`
(`RUN_ID` must be `[a-z0-9]`). Gates template is `training/cloudwatch/gates.json`.

## Hard invariants
1. **Production is read-only** until Step 10. Candidates are deployed as separate runtimes/stacks.
2. **One layer per round.** A candidate changes exactly one surface (prompt file, tool tier, tool description,
   workflow, skill, or model). Verify with `01_deploy.sh <arm> … --synth-only` that the template diff is only that.
3. **Gates are registered before any candidate result exists** (`09_gates.py register` refuses otherwise).
   Any later change must be recorded with time, reason, and `calibrated_after_results: true`.
4. **Ground truth is deterministic** (rules/oracle or human-confirmed). Never copy an agent answer into a dataset.
5. **Join by session id**, never by position. Unscored ≠ 0.
6. **The control arm is rebuilt from the same package as the candidate** (`01_deploy.sh b0 <baseline prompts>`, `compare --base b0`). A months-old production package can lack data the confirm set needs.
7. **Promotion requires explicit user approval** (`CONFIRM=yes`) and a PASS gate file.

## Steps
1. `00_prereq.sh` — tools, identity, Transaction Search ACTIVE, model access. If Transaction Search is off,
   ask the user before running with `--enable-tracing` (it changes account settings).
2. `01_deploy.sh baseline` — CDK deploy. Ask before deploying.
3. `02_verify_observability.sh baseline` — one invocation; spans must include `invoke_agent`, `execute_tool`, `chat`.
4. `03_dataset.sh control` (and later `confirm`) — create dataset + immutable version; read back and compare.
5. `04_traffic.sh baseline control <b>` for b = 1..B (B ≥ 2, ≥ 3 preferred), then `05_batch_eval.sh` for each.
6. `06_insights.sh baseline control 1 … B` — classify. Then **review traces yourself** and decide the layer
   with the table in `workflow.md §2` (insight `fixType` over-assigns `SYSTEM_PROMPT_FIX`). Present the decision
   with prevalence (k/B per scenario) and let the user choose the layer.
7. `09_gates.py calibrate` (A/A on two baseline batch sets) → `09_gates.py register --stage S0 --set control`.
   If the register step reports the gate is unreachable, stop and let the user choose: more batches, more scenarios,
   or a wider business margin — before any candidate runs.
8. `07_recommend.sh <cid> auto|manual …` — generate the candidate for the chosen layer only. Review the text against
   the policy source; reject candidates that encode wrong rules.
9. `01_deploy.sh <cid> <prompts.json>` (ask first) → `04/05` for the candidate with the **same** batches, interleaved
   with baseline batches when possible.
10. `09_gates.py compare --stage S0 --cand <cid> --set control --batches …`. If PASS: register S1 on `confirm`
    (before running it), run baseline + candidate on confirm, compare S1.
11. S2 (cloud canary with real traffic) and `10_promote.sh` only with explicit user approval.
12. `99_cleanup.sh` lists resources; delete only with user approval.

## Reporting rules
- Report counts and gate outcomes; never retype score tables by hand — quote the script output.
- Distinguish `FAIL` (measured, did not pass) from "no measurement" (0 sessions, missing files).
- When a result surprises you, check judge noise (A/A, re-score) before concluding.
