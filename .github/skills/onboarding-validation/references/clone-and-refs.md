<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Clone and Ref Handling

This file is part of the `onboarding-validation` skill instructions.

## Source checkout details

- Resolve the prompt's repository, ref (branch, tag, or commit), and app path. Log the requested ref and its resolved commit, then verify the fresh checkout with `git rev-parse HEAD`. Put the full SHA in the Summary.
- Inspect the guide's application clone and checkout instructions without rerunning them. Use the pinned checkout for the remaining documented steps, including dependency setup. Before launch, log the execution directory and verify `HEAD` still matches the pin. Do not launch from a different commit.
- **Rule 1.3:** Grade whether the guide explicitly selects a branch, tag, or commit without making the reader guess. A named moving branch can PASS. Record a difference from the prompt ref in Execution Notes, even if both refs resolve to the same SHA.
- **Rule 10.2:** A moving source ref in the documented path, including a release branch, is a **Major FAIL**, even if it matches the prompt or currently points to the pinned SHA. A different fixed release tag or commit is not a failure by itself.
- **Submodule apps.** If the app folder is a git submodule of the base repo (it appears in the base repo's `.gitmodules`), the version under test is the commit the base ref pins for that submodule — its **gitlink SHA** — which is the ground truth **regardless of what the submodule's own documentation says to clone**. The agent MUST:
  - Set `base_url`, `base_dir`, `base_sha` (the resolved suite commit), and `app_path` before running these commands. Stop if a command fails or either checkout differs from its pin. Do not switch to a branch tip or clone the submodule directly.
    ```bash
    git init "$base_dir"
    cd "$base_dir"
    git remote add origin "$base_url"
    git fetch --no-tags --filter=blob:none origin "$base_sha"
    git sparse-checkout set -- "$app_path"
    git checkout --detach "$base_sha"
    git rev-parse HEAD                               # must equal base_sha
    git ls-tree HEAD -- "$app_path"                   # mode 160000; record the gitlink SHA
    git submodule update --init --recursive -- "$app_path"
    git -C "$app_path" rev-parse HEAD                 # must equal the gitlink SHA
    ```
  - Record BOTH SHAs in the Summary: base `Commit` and `Commit (submodule)` = `<PINNED_SHA>`.
  - Evaluate the clone rules (1.x) against the **app's** documented get-started, which lives inside the submodule at `<PINNED_SHA>`.
  - Apply the same ref grading to the submodule's guide. Note any difference from `<PINNED_SHA>`; it does not change the app checkout under test.
