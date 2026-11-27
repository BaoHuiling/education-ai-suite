<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Evaluation Model Reference

This file is part of the `onboarding-validation` skill instructions.

## Evidence Collection

- **System inventory** (collect once at start, include in report header):
  ```bash
  # OS
  cat /etc/os-release | grep -E "^(NAME|VERSION)="
  # CPU
  lscpu | grep "Model name"
  # RAM (report raw value from free and physical size from dmidecode if available)
  free -h | grep Mem | awk '{print $2}'
  # Optional: if sudo available, get exact physical RAM
  sudo dmidecode -t memory 2>/dev/null | grep "Maximum Capacity" || true
  # GPU / NPU availability
  ls /dev/dri/render* 2>/dev/null && echo "GPU: available" || echo "GPU: not found"
  ls /dev/accel/accel* 2>/dev/null && echo "NPU: available" || echo "NPU: not found"
  # GPU model (if available)
  lspci | grep -i "vga\|display\|3d"
  ```
- **Clone transfer (1.2):** If the single checkout matches the documented procedure, capture received bytes attributable to that checkout using available transfer counters. Include required sparse materialization and source submodule transfers. Use N/A if the procedure differs or the transfer cannot be measured reliably. Do not reclone or substitute disk usage.
- Measure time: use `time` prefix on key commands.
- Check health based on deployment method:
  - Docker Compose: `docker compose ps`, `docker compose logs`
  - Helm/K8s: `kubectl get pods`, `kubectl logs`
  - Docker (single-container): `docker ps --filter name=<container>`, `docker logs <container>`
- Check resources based on deployment method:
  - Docker Compose: `docker stats --no-stream`, `df -h`
  - Helm/K8s: `kubectl top pods`, `df -h`
  - Docker (single-container): `docker stats --no-stream <container>`, `df -h`
- Verify endpoints using the URL documented by the application.
  Use the access mapping established by `remote-access.md`; distinguish validator
  access errors from application defects.

## Verdict per Rule

For each rule, report one of:

- **✅ PASS**: The application meets the rule.
- **❌ FAIL**: The application does not meet the rule. Include the reason and evidence.
- **⚪ N/A**: The rule does not apply, or the available evidence is insufficient to evaluate it. State which reason applies.

If a check cannot be executed, use N/A with the reason rather than inventing a verdict;
do not use it to hide an observed defect. Rule 7.6 always applies: N/A there means
functional output could not be evaluated, and prevents a positive Overall Result.

Evaluate each rule from its own evidence. A documented command that fails and
blocks the next required step is an observed defect; downstream runtime checks
that cannot be reached are N/A, not additional failures. A host limitation or
agent procedural error alone is not an application defect. Resolve documented
placeholders under `../SKILL.md` before execution; running them literally is an
agent error, not an application blocker. Required substitution still fails 5.3,
but does not stop validation or automatically imply Critical. Classify its actual
impact using the severity model below; unavailable inputs leave unreached checks N/A.

The Result cell in the Detailed Results table MUST be prefixed with the status emoji (`✅` / `❌` / `⚪`), joined to the word by a **non-breaking space** (Unicode U+00A0, not a regular space) — e.g. `✅` + U+00A0 + `PASS` — so the icon can never wrap onto its own line. GitHub Markdown supports neither cell background colors nor `<nobr>`; emoji + non-breaking space is the portable equivalent.

The Summary count table header MUST carry matching icons, each joined to its label by a non-breaking space: `✅ PASS`, `🔴 FAIL (Critical)`, `🟠 FAIL (Major)`, `🟡 FAIL (Minor)`, `⚪ N/A` (the `Total Rules` column stays plain).

> **Enforced.** `scripts/reconcile-report.sh` FAILS the report if any status icon in a table row is followed by a regular space instead of U+00A0. A regular space here is a common, easy-to-miss regression — the script catches it so the icon never wraps to its own line in the rendered table.

## Reconciliation Script Checks

`scripts/reconcile-report.sh` (relative to this skill file) runs in two modes. With `--emit-skeleton`
it writes the report skeleton — one Detailed Results row per rule, both tables, every anchor — from
the same constants it validates against (see `report-format.md`, *Report format contract*). Without
arguments it performs these checks, in order:

Each line containing `<!--` (including code blocks) emits a `WARNING` advising
removal of the unexpected report comment. This does not affect results or exit status.

1. Asserts the **Detailed Results** and **Summary count** table headers before reading any column, so an inserted or reordered column fails loudly instead of shifting the counts.
2. Requires the run-identity rows **`AI agent`** and **`Model`** to be present and non-empty.
3. Extracts rule IDs from the rules file (sections 1–16 only — it stops at the `## Rationale` heading) and from the report's Detailed Results table, and detects missing, extra, or duplicate rule IDs.
4. Counts per-category verdicts (PASS, Critical, Major, Minor, N/A) by reading the **Result and Severity columns only** — never the whole line — asserts every row resolves to exactly one verdict (and each FAIL to exactly one severity), and prints a `CHAT_SUMMARY:` line with the authoritative counts for the agent to reuse verbatim.
5. Verifies the sum equals the total rule count, AND cross-checks that the headline **Summary count table** matches the counted rows (catches drift even when the sum is still correct).
6. Enforces a **non-breaking space (U+00A0)** between each status icon and its label in table rows, so icons never wrap onto their own line in the rendered table.
7. Cross-checks the headline **Overall Result** in order: any Critical → FAIL; otherwise rule 7.6 not PASS → BLOCKED; otherwise any Major/Minor → CONDITIONAL PASS; otherwise PASS.
8. For BLOCKED, requires `**Overall UX Score**: N/A (BLOCKED)` and rejects a numeric score bar. Otherwise recomputes the **Overall UX Score** and band from the verdicts using `report-format.md`, rejecting mismatches or unmapped rules.
9. Exits with code 1 and prints `FAILED` if any check fails; prints `OK: Reconciliation passed.` on success.

## Severity Levels

Each observed application/documentation FAIL MUST be classified based on its **own observable impact on the documented path**, using the decision tree below. Apply explicit rule-specific severity requirements and the deterministic guards before the general tree.

### Classify recovery without changing the first-attempt verdict

1. **Identify the cause from original evidence.** A limitation of the validator
   alone is not an application defect. If a check cannot be evaluated, mark it
   N/A unless other evidence supports a verdict.
2. **Ask whether a first-time user could continue safely without the agent's
   correction.** Count only the guide and clear, safe actions available in the
   normal interface. Do not assume the user would infer a corrected command,
   replace a required step, or supply an undocumented prerequisite. If an
   observed defect on the required path makes such a correction necessary,
   record **FAIL (Critical)** on the rule for the root cause unless an explicit
   rule-specific severity applies. If a safe, actionable continuation exists
   without a correction, apply the rule's stated severity or the nonblocking
   branch of the decision tree. A security warning alone does not establish
   that continuation is safe; follow the access and trust checks.
3. **Use recovery evidence only for checks it still tests.** Keep first-attempt
   findings, including 10.1 where applicable. A verified functional result after
   recovery can satisfy 7.6, but cannot turn a Critical FAIL into a positive
   Overall Result. Record the distinction in Execution Notes.

### General decision tree

```
Did this defect block the next required step on the original path or cause an observed incorrect functional result?
  YES → Critical
  NO  → Was the defect found only through code/doc/config inspection?
          YES → Minor
          NO  → Major
```

| Severity | Decision criterion |
|----------|-------------------|
| **Critical** | This defect blocked the next required step or caused an observed incorrect functional result. |
| **Major** | This defect caused an observed nonblocking runtime issue, or an explicit rule-specific requirement assigns Major. |
| **Minor** | This nonblocking defect was found only through inspection of files, docs, or config, unless an explicit severity requirement overrides it. |

The agent MUST NOT assign severity based on opinion. It MUST reference the specific step where the failure was observed or state "found during inspection" for Minor items.

An unrelated blocker or an unexecuted 7.6 MUST NOT promote other findings to
Critical or Major. Missing functional verification alone is not evidence that
those defects caused it; retain N/A for unexecuted checks and classify observed
defects independently.

### Severity consistency (deterministic guards)

These guards remove judgment drift on identical facts (the same finding must not swing between Major and Critical across runs):

1. **Sample-input severity cap.** If rule **7.6 is PASS**, a missing or bring-your-own sample input under rules **12.1/12.2** is at most Major. This cap applies only to the sample-input finding; it does not downgrade an independent defect that blocked a required step.
2. **One root cause = one defect.** Do NOT record the same underlying gap as Critical on two different rules. Rules 12.1 and 12.2 are distinct: **12.1** = a ready-to-use sample input is bundled or auto-downloaded; **12.2** = a command or container to simulate the live input is documented. A documented simulator that requires the user to point it at their own file **still satisfies 12.2 (PASS)** — the absence of a bundled file is solely a **12.1** finding.
3. **Self-signed trust.** Evaluate rule 7.1's documentation requirement even if no
   trust error was observed; apply its Major classification only when its stated
   conditions are met. Follow the diagnostic procedure in `remote-access.md`.
   Do not count the same trust gap under UI rules. Recovered access can support
   7.6 PASS only if the functional result is verified; it does not erase a 7.1 FAIL.

## Overall Result Criteria

| Result | Condition |
|--------|-----------|
| **FAIL** | One or more Critical FAILs; takes precedence over BLOCKED. |
| **BLOCKED** | Zero Critical FAILs and rule 7.6 is not PASS. Functional output remains unverified; explain the limitation in Execution Notes. |
| **CONDITIONAL PASS** | Rule 7.6 is PASS, zero Critical FAILs, and one or more Major or Minor FAILs. |
| **PASS** | Rule 7.6 is PASS and zero FAILs at any severity. |

Apply these rows in order, after classifying the evidence. The checker enforces
the Overall Result for the supplied verdicts and severities; it does not verify
their evidence or assign severity.

> **Why the count is canonical — and there is NO deduplication.** The canonical rule count is whatever `scripts/reconcile-report.sh` extracts — currently **76**. The script counts only the rule rows in sections 1–16; it stops at the `## Rationale for Key Thresholds` heading, so those explanatory rows are never counted (regardless of how that table is formatted). The script does **NOT** deduplicate rule IDs — a duplicate ID is an error it reports, not something it silently merges. The agent MUST NOT explain the count by claiming IDs are "deduplicated."
