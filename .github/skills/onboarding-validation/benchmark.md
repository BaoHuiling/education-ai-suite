<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Benchmark: onboarding-validation

| Field | Value |
|-------|-------|
| Skill version | 1.14.0 |
| Rules version | 1.4.0 |
| Date | 2026-11-12 |
| Status | Manual validation complete; automated benchmark pending |

## Summary

The skill has been validated through 2 runs against 2 distinct OEP applications using docker-compose deployment. Formal automated benchmark via `skill-creator` Stage 5–7 is pending.

## Validation Results

| Application | Commit | Deployment | AI agent | Model | Overall Result | UX Score | Date |
|-------------|--------|-----------|----------|-------|----------------|----------|------|
| live-video-captioning | `c645ac49` | docker-compose | not recorded (pre-1.13.0) | not recorded (pre-1.13.0) | CONDITIONAL PASS | 8.7 / 10 — Good | 2026-07-21 |
| handheld-multi-modal | `0bdf172c` | docker-compose | not recorded (pre-1.13.0) | not recorded (pre-1.13.0) | CONDITIONAL PASS | 8.1 / 10 — Good | 2026-07-21 |

Run identity (`AI agent`, `Model`) became a mandatory Summary field in skill 1.13.0. Earlier runs are
kept as-is rather than back-filled from memory; every run from 1.13.0 on records both values.

## Eval Coverage

| Eval ID | Scenario | Type | Status |
|---------|----------|------|--------|
| 1 | Docker Compose app (metro-ai-suite) | should_trigger | Validated manually |
| 2 | Helm/K8s app (metro-ai-suite) | should_trigger | Not yet executed |
| 3 | Debugging request (negative) | should_not_trigger | Defined |
| 4 | Submodule app (retail-ai-suite) | should_trigger | Not yet executed |
| 5 | Code review request (negative) | should_not_trigger | Defined |
| 6 | Skill integrity self-test (negative trigger) | should_not_trigger | Defined |

## Eval coverage policy

Evals cover classes of skill behavior, not an inventory of applications. Application scenarios use `example-prompts/01-validate-onboarding.md`. Defined scenarios are not executed tests.

## Skill Integrity Checks

`scripts/self-test.sh` runs offline, needs no access to a validated application, and must pass after
any change to the skill.

| Check | Method | Result |
|-------|--------|--------|
| Golden fixture reconciles cleanly (76 rules) | `scripts/self-test.sh` [1/6] | PASS |
| Contract mutations are detected (missing rule, summary drift, wrong UX score, U+00A0, column insert, missing run identity) | `scripts/self-test.sh` [2/6] | PASS |
| Rule 7.6 gates positive results; BLOCKED suppresses UX score; Critical FAIL retains precedence | `scripts/self-test.sh` [3/6] | PASS |
| Generated skeleton covers exactly one row per rule, and an unfilled skeleton is rejected | `scripts/self-test.sh` [4/6] | PASS |
| Format contract in the checker matches `references/report-format.md` | `scripts/self-test.sh` [5/6] | PASS |
| All reference files reachable from SKILL.md, all relative links resolve | `scripts/self-test.sh` [6/6] | PASS |
| Structure & spec compliance | `skill-validator check` | PASS |
| Instruction quality (clarity / actionability / novelty) | `skill-validator score evaluate` | *(result)* |
