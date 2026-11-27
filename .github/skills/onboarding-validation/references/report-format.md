<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Report Format Reference

This file is part of the `onboarding-validation` skill instructions.

## Report format contract

`scripts/reconcile-report.sh` is both the **producer** and the **consumer** of this template: run with
`--emit-skeleton` it writes the report skeleton, run without arguments it validates a filled-in
report. Both modes read the same constants, so the structure an agent fills in and the structure the
checker expects cannot drift apart.

The constants below are declared once at the top of `scripts/reconcile-report.sh` and nowhere else.
This block is the contract, copied **verbatim** from the script; `scripts/self-test.sh` fails if the
two ever disagree.

```bash
RULE_ROW_RE='^[|] [0-9]+[.][0-9]+ [|]'
SUMMARY_ROW_RE='^[|] *[0-9]+ *[|] *[0-9]+ *[|] *[0-9]+ *[|] *[0-9]+ *[|] *[0-9]+ *[|] *[0-9]+ *[|]'
OVERALL_RESULT_RE='^\*\*Overall Result\*\*:'
UX_BAR_RE='^[█░]+ [0-9]+\.[0-9]+ / 10'
UX_INLINE_RE='^\*\*Overall UX Score\*\*:'
RATIONALE_STOP_RE='^## Rationale'
AGENT_ROW_RE='^[|] *AI agent *[|]'
MODEL_ROW_RE='^[|] *Model *[|]'
UX_DIM_ROW_RE='^[|] *D[0-9]+ *[|]'
DETAIL_HEADER_RE='^[|] *ID *[|] *Rule \(short\) *[|] *Result *[|] *Severity *[|]'
SUMMARY_HEADER_RE='^[|] *Total Rules *[|]'
COL_ID=2
COL_SHORT=3
COL_RESULT=4
COL_SEVERITY=5
COL_VALUE=3
SUMMARY_COL_FIRST=2
ICON_PASS='✅'
ICON_FAIL='❌'
ICON_NA='⚪'
ICON_ANY_RE='(✅|❌|⚪|🔴|🟠|🟡)'
SEV_CRITICAL='Critical'
SEV_MAJOR='Major'
SEV_MINOR='Minor'
RESULT_FAIL='FAIL'
RESULT_CONDITIONAL='CONDITIONAL PASS'
RESULT_PASS='PASS'
RESULT_BLOCKED='BLOCKED'
BAND_EXCELLENT='Excellent'
BAND_GOOD='Good'
BAND_FAIR='Fair'
BAND_POOR='Poor'
BAND_VERY_POOR='Very Poor'
```

What each group binds to:

| Group | Binds to |
|-------|----------|
| `RULE_ROW_RE`, `RATIONALE_STOP_RE` | rule rows in Detailed Results and in the rules file; end of the rule set |
| `SUMMARY_ROW_RE`, `SUMMARY_HEADER_RE`, `SUMMARY_COL_FIRST` | the six-integer Summary count table |
| `DETAIL_HEADER_RE`, `COL_ID`, `COL_SHORT`, `COL_RESULT`, `COL_SEVERITY` | the Detailed Results table and its column order |
| `AGENT_ROW_RE`, `MODEL_ROW_RE`, `COL_VALUE` | the run-identity rows of the Summary table |
| `OVERALL_RESULT_RE`, `RESULT_*` | the headline verdict line and its vocabulary |
| `UX_BAR_RE`, `UX_INLINE_RE`, `BAND_*` | the Overall UX Score line, current and pre-1.11.0 form |
| `UX_DIM_ROW_RE` | rows of the canonical UX dimension table below |
| `ICON_*`, `SEV_*` | verdict and severity vocabulary, and the U+00A0 rule |

The column positions are asserted through `DETAIL_HEADER_RE` and `SUMMARY_HEADER_RE` before any
value is read, so a reordered or inserted column fails loudly instead of shifting the counts.

Changing any constant requires a `Skill Version` bump plus an update to this block and to the
checker. Both are covered by `scripts/self-test.sh`, which also runs the checker against the golden
fixture in `assets/sample-report.md` and against deliberately mutated copies of it.

## Output Format

The agent MUST NOT hand-write the report structure. Create the report from the bundled generator,
then fill in the values. Run from the skill directory:

```bash
export RULES_FILE="<absolute-path-to-this-skill>/references/rules-onboarding-validation.md"
export REPORT_FILE="<absolute-path-to-new-report>"
(set -C; ./scripts/reconcile-report.sh --emit-skeleton > "$REPORT_FILE")
```

The skeleton already contains one Detailed Results row per rule (so no rule can be forgotten), both
tables, and every anchor the checker looks for. The agent fills in the Summary values, the verdicts,
the evidence, and the narrative sections.

The agent MUST save the report as a markdown file — NOT display it inline in chat. The file MUST be saved to:

```text
./validation-reports/<application>-<date>-<commit8>.md
```

Where `<application>` and `<date>` are the values from the report's Summary table (Application and Date fields), and `<commit8>` is the first 8 characters of the validated Commit SHA (for a git-submodule app, use the submodule's pinned SHA — it identifies the app version under test).

Create the output directories if needed. Choose one unused stem for both files; if either path exists (including symlinks) or creation detects a collision, try `-run2`, `-run3`, etc. Use no-clobber creation for both; never overwrite or append to prior artifacts. Report other write errors and edit or reconcile only this run's report.

### Process log (second artifact)

Every run produces **two** files: this report **and** a process log — the terminal transcript started in Execution step 1. After finishing the run and closing the interactive transcript or completing all non-interactive `script` invocations, the agent MUST copy the flushed transcript to:

```text
./validation-logs/<application>-<date>-<commit8>.log
```

Use the **same stem** as the report, including any run suffix. With `LOG_FILE` set to the output path, copy using `(set -C; cat "$RUN_LOG" > "$LOG_FILE")`. Every PASS/FAIL must be traceable through the log to actual evidence: command and output, a browser action and its captured result, or a cited file/line for inspection. For browser evidence, log the observer, action and evidence reference; do not use a successful terminal probe as evidence of browser output. If evidence is insufficient, mark the rule **N/A** and explain in Execution Notes.

Before sharing, scan both files locally for common secret indicators (e.g., private key headers, bearer tokens, and credential fields). Use an available local scanner or text search that returns only file names and line numbers, not matched text. Do not load either file into the model for a full reread. Resolve flagged lines locally and redact sensitive values while preserving verdict evidence. Withhold any artifact if the check fails or a finding cannot be resolved safely. Reassess affected verdicts and reconcile again only if redaction leaves insufficient evidence.

The report MUST use the following structure. It MUST begin with a top-level title — `# Onboarding Validation Report: <App Display Name>` — where `<App Display Name>` is the application's full name from its README (the top `#` heading), e.g. `# Onboarding Validation Report: Live Video Captioning`. The remaining sections follow in this order:

The skeleton defaults to BLOCKED. Retain its alert immediately below the title for
BLOCKED reports; remove it otherwise:

```markdown
> [!CAUTION]
> **BLOCKED — functional output has not been verified (rule 7.6).**
```

This uses the renderer's warning styling, not unsupported custom background colors.

### Summary

| Field | Value |
|-------|-------|
| Rules Version | X.Y.Z |
| Skill Version | X.Y.Z |
| AI agent | The harness that executed this run, e.g. `GitHub Copilot coding agent (VS Code)`, `Claude Code CLI`. |
| Model | The model identifier the harness used, e.g. `claude-sonnet-4.5`. |
| Date | YYYY-MM-DD |
| Application | kebab-case name, e.g., `live-video-captioning` |
| GitHub URL | The URL from the validation prompt |
| Commit | Full 40-character SHA from `git log -1 --format='%H'` after clone |
| Commit (submodule) | Only when the app is a git submodule: its pinned gitlink SHA (`git -C <submodule-path> rev-parse HEAD`). Omit this row otherwise. |
| Deployment method | docker-compose / helm / docker |
| OS | e.g., Ubuntu 24.04.4 LTS |
| CPU | e.g., Intel Core Ultra 7 265K |
| RAM | e.g., 94 Gi available (96 GB physical) |
| GPU | e.g., Intel Arc A770 (`/dev/dri/renderD128` present) or "not found" |
| NPU | e.g., available (`/dev/accel/accel0` present) or "not found" |
| Documentation path followed | Ordered list of document(s) and section(s) the agent read to complete the installation. Example: `1. README.md → "Quick Start Guide"  2. docs/prerequisites.md → "GPU Driver Setup"`. List every page and heading the agent had to visit — this shows the complexity of the documentation trail. |

**Run identity.** `AI agent` and `Model` are **mandatory** — the checker fails the report if either
row is missing or empty. They make two runs of the same application comparable: the same model in a
different harness (different tool access, different auto-approval policy) does not produce the same
onboarding experience. Both values are **self-reported**: the agent MUST record what it actually
knows about itself and MUST NOT guess a version number. If the model identifier is not available to
the agent, write `unknown (self-reported)`. The agent also echoes both values into the process log
(Execution step 1) so the Evaluation layer can see them in the transcript.

**Overall UX Score**

```text
██████████████████████████████████████████░░░░░░░░ 8.4 / 10 — Good
```

The UX score bar uses Unicode block characters: `█` for filled positions and `░` for empty. Always **50 characters** total. Filled count = `round(score * 5)` (each character = 0.2 points; score 8.4 → 42 filled + 8 empty). The score value and band follow on the same line. The bar MUST be in a fenced code block for consistent monospace rendering.

For BLOCKED, replace the heading and score bar with
`**Overall UX Score**: N/A (BLOCKED)` and leave dimension ratings N/A.

| Total Rules | ✅ PASS | 🔴 FAIL (Critical) | 🟠 FAIL (Major) | 🟡 FAIL (Minor) | ⚪ N/A |
|-------------|------|-----------------|-----------------|-----------------|-----|
| X           | X    | X               | X               | X               | X   |

**Overall Result**: PASS / CONDITIONAL PASS / FAIL / BLOCKED

### User Experience Summary

A reader-facing view of how hard the application is to onboard. See "User Experience Score" for the model. This section has two tables. Use **plain text** for bands — do NOT use status icons here, so the U+00A0 rule does not apply.

**Measured UX Facts** — use timings and logs from this run. Do not reclone, shape traffic, or change assets to measure performance.

- Log when checkout starts and when required source is ready, including sparse materialization. If required submodules are fetched later during setup, report that time separately when known. Label a different clone procedure as **validator checkout**.
- Separate model, sample-data, and image downloads from local preparation and startup when the logs support it. Note what was cached. If phases are bundled, report their combined elapsed time once; do not estimate individual timings.
- Report first useful result as wall-clock time from the earliest of source checkout or required documented preparation performed in this run to the first observed result defined by 7.6. List any required preparation not timed (e.g., Docker was already installed) and label the total as partial; do not rerun or estimate it. Note material validator-only pauses, report an upper bound if the first result was detected by polling, and do not add overlapping phase times. If no result was verified, record N/A and the reason.

| # | Metric | Value | Target | Source |
|---|--------|-------|--------|--------|
| 1 | Clone size | … | ≤ 100 MB | rule 1.2 |
| 2 | Source checkout time | … | < 2 min (advisory) | rule 1.5 |
| 3 | Get-started steps | … | ≤ 4 | rule 5.1 |
| 4 | Start commands | … | 1 | rule 4.2 |
| 5 | App start (deploy) | … | < 5 min | rule 4.1 |
| 6 | First-run downloads and preparation | … | n/a | process log |
| 7 | Time to first useful result | … | n/a | process log / rule 7.6 |
| 8 | Image size | … | ≤ 30 GB | rule 9.1 |
| 9 | Peak RAM | … | ≤ 80% of min | rule 9.2 |
| 10 | External tools | … | ≤ 3 | rule 2.2 |
| 11 | UI ready | … | ≤ 60 s | rule 7.2 |
| 12 | Minimum skill level | A / B / C | B | rule 11.6 |

**UX Dimension Scores** — the seven dimensions, each a rating 1–10, its band, and the rule verdicts behind it:

| Dim | Name | Rating (1–10) | Band | Rule basis (non-N/A) → points/max | Notes |
|-----|------|---------------|------|-----------------------------------|-------|
| D1 | Time-to-Deploy | … | … | … | … |
| D2 | Setup Effort & Steps | … | … | … | … |
| D3 | Prerequisites & Footprint | … | … | … | … |
| D4 | Documentation & Skill | … | … | … | … |
| D5 | Reliability & Reproducibility | … | … | … | … |
| D6 | Cleanup, Security & Observability | … | … | … | … |
| D7 | UI & Interaction | … | … | … | … |

### Detailed Results

Use the `Short` column from the rules tables as the rule label:

| ID | Rule (short) | Result | Severity | Evidence / Reason |
|----|--------------|--------|----------|-------------------|
| 1.1 | Partial clone used | ✅ PASS / ❌ FAIL / ⚪ N/A | Critical/Major/Minor/— | ... |
| ... | ... | ... | ... | ... |

In **Evidence / Reason**, use inline Markdown links (`[label](URL)`) in the cell, with a descriptive label and a brief explanation of what the evidence shows. Do not use reference-style links (`[label][id]`), end-of-report link definitions, or citation keys. For example: `[README: Quick Start, line 25](https://github.com/owner/repo/blob/<commit>/README.md?plain=1#L25) — lists the required setup steps`.

Pin GitHub file links to the validated commit and anchor to the cited lines when known. For Markdown files, add `?plain=1` before the anchor to open the source view. Use relative links for local artifacts available alongside the report. If no link is available, state the evidence or reason directly; do not invent a link.

### Critical Issues

Name every rule marked Critical FAIL in Detailed Results and no other rules. Group related failures by root cause; the number of items need not match the number of failures.

### Recommendations

Group suggested fixes by severity (Critical first, then Major, then Minor). Cover every FAIL, and reference every rule ID addressed by each recommendation. Related failures may share a recommendation.

### Execution Notes

Document any procedural issues (agent retries, timing anomalies, network issues unrelated to the app).

For bounded recovery, record the original instruction and observed error (or
why it was unsafe to run), the exact change and its justification, the recovery
result, and affected rule IDs. Label downstream evidence collected after recovery.

Record verification-readiness probes, deferred app-dependent checks and capture
timing. For an early stop, name the missing capability/input and costly steps
not attempted; distinguish validator limitations from app/documentation defects.

For remote/browser checks, record the evidence-backed access mapping actually
used, probe results, corrections and cleanup as required by `remote-access.md`.
For TLS diagnostics, record the error, documented trust instructions (or their absence),
compared public certificate fingerprints and their sources, trust scope, and outcome.
Never include private keys.

## User Experience Score (MANDATORY)

Except for BLOCKED reports, the agent MUST compute a single **Overall UX Score (1–10)** plus a per-dimension breakdown, and present them in the report's **User Experience Summary** section. The score is **fully derived from the rule verdicts** — it adds no subjective input (Charter principle 1, *evidence over opinion*). It is deterministic: the same verdicts always yield the same score, and `scripts/reconcile-report.sh` recomputes it from the Detailed Results table and FAILS the report if the declared value does not match. BLOCKED reports have no numeric score or band.

### Dimensions and weights (canonical)

Every rule belongs to **exactly one** of seven UX dimensions — a strict partition of all 76 rules, so no rule is counted twice. This table is the **single source of truth**: `scripts/reconcile-report.sh` parses it directly (anchor `UX_DIM_ROW_RE`) instead of keeping a copy, and reports any rule ID in the report that this table does not cover. When the rules file changes, update this table — there is nothing else to keep in sync.

| Dim | Name | Weight | Rule IDs |
|-----|------|--------|----------|
| D1 | Time-to-Deploy | 2.0 | 1.5, 4.1, 4.3, 4.4, 4.5, 7.2 |
| D2 | Setup Effort & Steps | 2.0 | 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.2, 5.1, 5.2, 5.3, 5.4, 5.5 |
| D3 | Prerequisites & Footprint | 1.5 | 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 6.1, 6.2, 6.3, 6.4, 9.1, 9.2, 9.3, 12.3 |
| D4 | Documentation & Skill | 1.5 | 7.1, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8 |
| D5 | Reliability & Reproducibility | 2.5 | 7.3, 7.4, 7.5, 7.6, 10.1, 10.2, 10.3, 10.4, 12.1, 12.2, 15.1, 15.2, 15.3, 15.4 |
| D6 | Cleanup, Security & Observability | 1.0 | 8.1, 8.2, 8.3, 13.1, 13.2, 13.3, 13.4, 14.1, 14.2, 14.3 |
| D7 | UI & Interaction | 1.0 | 16.1, 16.2, 16.3, 16.4, 16.5 |

D5 carries the highest weight because it contains `10.1 First-attempt success` and `7.6 Functional output verified` — deploying and producing a verified result is the most important onboarding outcome.

### Points per verdict

| Verdict | Points |
|---------|--------|
| ✅ PASS | 1.00 |
| ❌ FAIL (Minor) | 0.50 |
| ❌ FAIL (Major) | 0.25 |
| ❌ FAIL (Critical) | 0.00 |
| ⚪ N/A | excluded from the dimension's denominator |

### Formula

1. **Per-dimension sub-score**: `sub(D) = Σ points(non-N/A rules in D) / count(non-N/A rules in D)`. A dimension whose rules are all N/A is itself N/A and its weight is dropped from the overall.
2. **Per-dimension rating (display only)**: `rating(D) = 1 + 9 × sub(D)`, rounded half-up to 1 decimal. Shown in the report's Table B.
3. **Overall raw score**: `raw = 1 + 9 × [ Σ weight(D)·sub(D) / Σ weight(D) ]` over non-N/A dimensions. Computed from **unrounded** sub-scores — so the rounded per-dimension ratings need NOT re-sum to the overall (this is expected, not an error).
4. **Severity caps** (keep the score consistent with the Overall Result):
   - any Critical FAIL ⇒ `score ≤ 4.0` (Overall Result is FAIL);
   - zero Critical and ≥1 Major/Minor ⇒ `score ≤ 8.9` (CONDITIONAL PASS — never reaches Excellent);
   - zero FAILs ⇒ `score = 10.0` (PASS).
5. **Rounding**: half-up to 1 decimal.
6. **No floor.** A formally-passing app (CONDITIONAL PASS) with many defects MAY land in a low band. The score reports UX honestly and is allowed to be lower than the three-level Overall Result would suggest.

### Bands

| Score | Band |
|-------|------|
| 9.0–10.0 | Excellent |
| 7.0–8.9 | Good |
| 5.0–6.9 | Fair |
| 3.0–4.9 | Poor |
| 1.0–2.9 | Very Poor |

The band MUST stay consistent with the Overall Result: PASS ⇒ Excellent (10.0); CONDITIONAL PASS ⇒ at most Good; FAIL ⇒ at most Poor.
