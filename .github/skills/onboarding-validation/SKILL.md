---
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
name: onboarding-validation
description: >-
  Validate the get-started experience of Open Edge Platform (OEP) software
  components from the perspective of a first-time user. Use this skill when a
  user wants an AI agent to follow onboarding or deployment documentation
  exactly, validate a Docker Compose or Helm/Kubernetes path, collect evidence,
  apply pass/fail rules, and produce a structured onboarding validation report
  with a process log. Trigger on onboarding validation, first-time-user
  validation, documentation-driven deployment checks, reproducibility checks,
  UX scoring, and release-readiness reviews for OEP components including Edge
  AI Suite and Edge AI Libraries applications. Do not use this skill for
  debugging, fixing application code, or ad hoc exploratory testing outside
  the documented path.
license: Apache-2.0
compatibility: >-
  Requires a bash-compatible shell, git, and access to the target environment.
  The validated application may additionally require Docker Compose or
  Helm/Kubernetes, depending on the documented deployment method.
  For browser verification, prefer MCP Playwright. The VS Code integrated browser
  is an opt-in fallback; see references/remote-access.md for the selection policy.
metadata:
  author: open-edge-platform
  version: "1.21.0"
  tags: validation, onboarding, qa, docker-compose, helm, kubernetes, edge-ai
---

# User Onboarding Experience Validation

Validate the get-started experience of a containerized application from the perspective of a first-time user. The agent follows the documentation exactly, collects evidence, evaluates pass/fail rules, and produces a structured report plus a verbatim process log.

| Field | Value |
|-------|-------|
| Skill ID | onboarding-validation |
| Version | 1.21.0 |
| Date | 2026-11-25 |
| Trigger | Validation prompt (see `example-prompts/01-validate-onboarding.md`) |
| Input | GitHub URL of application + deployment method |
| Output | Markdown report in `./validation-reports/` + process log in `./validation-logs/` |
| Rules | `references/rules-onboarding-validation.md` (**normative**) |
| Charter | `references/rules-charter.md` (**normative**) |
| Checker | `scripts/reconcile-report.sh` (also generates the report skeleton) |
| Benchmark | `benchmark.md` — validation runs, eval coverage, open gaps (maintainers only; not read during a run) |

> **Inherits `references/rules-charter.md`.** This skill implements the bundled charter. If the two conflict, **the charter wins**.

---

## When to Use

Use this skill when the user wants to:

- Validate the get-started experience of a containerized application as a first-time user.
- Check whether Docker Compose or Helm/Kubernetes onboarding documentation is reproducible.
- Produce a structured onboarding report with per-rule PASS / FAIL / N/A verdicts.
- Audit release readiness or onboarding UX for Open Edge Platform (OEP) software components, including Edge AI Suite and Edge AI Libraries applications.

Do not use this skill to:

- Debug, patch, or improve the application under test.
- Explore undocumented alternative deployment paths.
- Perform general code review that is unrelated to the documented onboarding path.

---

## Purpose

Validate the get-started experience of Open Edge Platform (OEP) software components from the perspective of a first-time user. The agent follows the documentation exactly and reports pass/fail for each rule.

---

## References

Read all files listed below in full before starting step 1; they are part of this skill's instructions, not optional background.

| File | Purpose | Status |
|------|---------|--------|
| `references/rules-onboarding-validation.md` | Pass/fail criteria (76 rules) | **normative** |
| `references/rules-charter.md` | Non-negotiable principles | **normative** |
| `references/evaluation-model.md` | Evidence model, verdict semantics, severity, overall result, checker logic | required |
| `references/report-format.md` | Report structure, UX scoring model, formatting contract | required |
| `references/clone-and-refs.md` | Branch/tag mismatch and submodule ref handling | required |
| `references/remote-access.md` | Target baseline, observer readiness, forwarding, TLS and media access | required |

---

## Instructions

### Execution Procedure

The agent MUST follow this procedure to avoid using stale or pre-existing workspace state:

Application rules grade the documented experience; they do not authorize extra runtime restrictions or skipping documented steps to satisfy a criterion.

1. **Work in an isolated directory and start the process log.** Create a fresh directory outside the workspace; stop if creation or entry fails. Record non-sensitive commands and output. Secret protection takes precedence over recording: run steps that may reveal secrets through a secure, unrecorded channel, with tracing off. Log only a redacted note and safe evidence. If no such channel is available, do not run the step. For an interactive terminal:
   ```bash
   WORK_DIR="$(mktemp -d /tmp/validation-<app-name>-XXXXXXXX)" && cd "$WORK_DIR"
   RUN_LOG="$WORK_DIR/run.log"
   script -q -f "$RUN_LOG"        # type `exit` to close and flush the recording
   echo "=== Run identity: agent=<harness> model=<model id or 'unknown (self-reported)'> ==="
   ```
   For non-interactive tools, append non-sensitive commands and output with `script -q -e -f -a -c '<command>' "$RUN_LOG"`. Start with the run identity; use the same `AI agent` and `Model` values in the report and do not invent a model version. Mark phases and log brief notes for severity calls, skips, and retries. Save the log as specified in `references/report-format.md`.
2. **Clone the pinned source once.** Use a fresh checkout of the prompt's ref and enter the selected application path. Use **Name** to select the sub-app if needed. Time the checkout. Inspect and report the guide's application clone command as written; do not rerun it. Follow `references/clone-and-refs.md` to verify source identity and handle submodules.
3. **Use only the cloned documentation.** After cloning, the agent MUST read and follow get-started instructions exclusively from the cloned repository — not from any workspace copy. This ensures the tested docs match the tested code.
   - **Documentation path selection.** The agent MUST scan the application's root `README.md` **from top to bottom** and select the **first section** whose heading clearly serves the purpose of guiding a new user through installation and first run. Common headings include "Get Started", "Getting Started", "Quick Start", "Quickstart", "Installation", "Setup", "Deploy", "Deployment", "Deployment Options", or similar — the exact wording may vary, but the intent must be unambiguous. The agent MUST NOT skip ahead to a shorter path or cherry-pick a different section — this tests the experience of a real first-time user who reads from the top. If a simplified quick-start exists below the fold but the first installation section is a full get-started guide, the agent follows the full guide and notes the quick-start in "Documentation path followed".
   - **Record every document visited.** As the agent follows the instructions, it MUST record every documentation page and section heading it reads, in order. This list goes into the report's "Documentation path followed" field. It reveals how many pages and sections the user must navigate to deploy — a concrete measure of onboarding complexity.
4. **Resolve inputs and prepare the target and observer.** After reading the guide, apply Documented Input Resolution and follow the target baseline and observer readiness checks in `references/remote-access.md` before running dependent steps.
5. **Run and verify in documented order.** Log times for preparation, startup, and the first verified result as available. Record the original outcome before any correction. Continue only where prerequisites hold. Repeat a step only to correct an agent error or under [Bounded recovery](#bounded-recovery); record why in Execution Notes. Do not restart the run or replay a trigger. Verify the result before teardown.
6. **Clean up.** Use only the application's documented teardown for resources created by this run; record any extra cleanup required as evidence for rule 8.2. Clean up validator access separately as specified in `references/remote-access.md`.

### Limit redundant work

- After selecting the guide, read only material needed for its steps or an applicable rule. Search for relevant sections or log lines before opening large files or outputs; load more context when needed to support a verdict.
- Reuse evidence from this run across rules. Avoid repeating reads, probes, screenshots, or downloads unless the state changed or the evidence is insufficient. Keep diagnostic output short and noninteractive (e.g., `git --no-pager log -1`) while retaining required evidence in the process log.
- Write the report from the bundled skeleton, update related evidence together, and avoid extra artifacts unless needed for traceability. These limits never justify skipping a documented step, required check, report row, or final reconciliation.

### Documented Input Resolution

- Resolve requested values before execution: use the prompt's value, otherwise the guide's default, recommendation or sole applicable example, in that order; ask when ambiguous. Preserve the source pin and use `references/remote-access.md` for addresses. Change only requested values, safely quoted, not command behavior.
- When the guide requests user-chosen local credentials, choose run-local names and cryptographically generate fresh passwords within documented constraints; reuse them consistently. External credentials require an authorized secure source, never invented tokens or secrets requested in chat. Keep secret values out of the transcript, input logging, tracing and output; use environment references or the documented secret-input mechanism.
- Scope restrictive permissions to secret files, e.g. create them in a subshell with `umask 077`; validator-side secret handling MUST NOT change permissions inherited by application setup or shared runtime assets.
- Continue deployment and functional verification after substitution. Record placeholders, sources and non-secret values in the log and Execution Notes; for secrets, record only variable names and provenance. Still assess required substitutions under 5.3 and, as applicable, 3.1/13.2. If an input is unavailable, record that limitation rather than executing an unresolved placeholder.

### Bounded recovery

One safe recovery attempt per failed step is permitted when the evidence identifies a case below and recovery can unblock further checks. Retry only if it cannot duplicate side effects or consume input twice. Do not chain fixes or replace the first-attempt evidence.

- **Unambiguous documentation typo:** correct only the command text (for example, fix the spelling of an argument whose intended form is confirmed by the pinned checkout or tool help). Do not execute an unsafe command just to observe failure; cite the defect and explain why it was not run.
- **Same artifact, different download method:** use an authorized client or a verified official alternate URL for the exact documented artifact/version. Preserve the expected destination and verify identity using a documented checksum, signature, or immutable official reference. Do not use an unverified mirror, infer a new version, or bypass authentication or integrity checks.
- **Validator access:** use only the endpoint mapping, scoped TLS handling, or browser fallback in `references/remote-access.md`. Follow its readiness and consent checks. Switching observers does not permit replaying an application action.

Do not edit source, deployment manifests, application ports, or undocumented configuration; do not swap inputs, models, or deployment methods, increase permissions, or weaken security controls. If the correction is ambiguous, unsafe, or fails, continue only with independent checks.

---

## Report workflow (MANDATORY)

1. **Generate.** From this skill directory, use the bundled generator and no-overwrite procedure in [report-format.md](references/report-format.md). Do not hand-write the template or reuse a prior run's files.
2. **Fill.** Complete every generated rule row, using **N/A** where appropriate, and derive all Summary counts from Detailed Results. Follow [evaluation-model.md](references/evaluation-model.md) for severity and overall result and [report-format.md](references/report-format.md) for layout and narrative coverage.
3. **Reconcile.** From this skill directory, run `./scripts/reconcile-report.sh` with `RULES_FILE` and `REPORT_FILE` set as for generation. Fix discrepancies and rerun until it exits successfully with `OK: Reconciliation passed.` Do not finalize the report before it passes.
4. **Quote.** In chat, quote the final checker's `CHAT_SUMMARY:` line verbatim. Do not recalculate its counts or change any reported rule severity.
