<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Verification Readiness and Remote Access

## 1. Prepare the target and observer

### Docker baseline

- **Before pulling, building, or launching**, confirm the Docker daemon the documented commands will use. If the guide first installs or starts Docker, check as soon as that daemon is reachable.
- **Inspect read-only and log the baseline.** Check for running or stopped containers, local images, volumes, bind-mount data, networks, and build cache the selected path could reuse or conflict with. Limit checks to state that could affect the first-run result.
- **If relevant state is found**, use an authorized clean target or stop the affected steps. Do not remove or prune existing resources or change the documented commands.
- **If a baseline check is inconclusive**, log what could not be verified and why. Ask once whether to continue with safe affected steps on the unverified target or skip them, unless the user already opted in. Without approval, skip the affected steps and continue independent checks. Record the decision in Execution Notes. Approval does not permit unsafe steps or change grading: mark affected rules **N/A** when prior state prevents a reliable verdict. Treat the uncertainty as an environment limitation, not an app defect.

### Observer readiness

1. **Before costly setup, downloads, builds, or deployment:** use rule 7.6 to identify the result and trigger. Check only the required channels. Exercise each observer in its actual context with a safe sample; checking its version is not enough. For browser checks apply the selection policy below.
2. **Before a one-shot trigger:** have capture ready, even if deployment itself triggers processing. Defer only checks requiring app-created resources. At the earliest documented opportunity, verify the actual endpoint or output from the intended observer, including TLS, authentication, and media where required. Correct validator access without reconfiguring or replaying the application.
3. **If a required capability is missing:** try supported, authorized alternatives within the same observer requirements. Repeat a probe only when new evidence justifies it. Ask only for a specific input or authorization that enables a verifiable method. If still blocked, skip costly dependent work, continue independent checks, and finalize the report. Record deferred and unexecuted checks as N/A as appropriate; validator limitations are not application defects.

### Browser selection

Prefer MCP Playwright. Test navigation, interaction, and evidence capture; verify app-dependent capabilities (such as media) when the endpoint exists. If MCP is unavailable or cannot collect the required evidence, report the reason and any compatible setup option. Do not install tools automatically or weaken sandbox security; retest if the user equips MCP.

If the integrated VS Code browser can plausibly collect the required evidence, ask once whether to use it or stop browser-dependent work to prepare MCP. Do not ask again if the prompt already granted permission; respect any explicit opt-out. After approval, verify that VS Code can operate in the required context and collect equivalent evidence; never assume access from a user's manual browser session. If the user declines, no decision can be obtained, or VS Code cannot verify the result, apply the readiness-stop procedure. Do not ask about browser choice for non-browser paths. Record the selected observer, any consent, and its evidence or limits.

## 2. Establish access

- **Locate deployment and observer separately.** Check the selected runtime, published endpoint or output location, and the observer's network or filesystem context. Docker context/host overrides, a mounted socket, or a remote cluster can put deployment outside the shell's context. If deployment is on the terminal host, corroborate the server address from `SSH_CONNECTION` with the host's route and addresses; otherwise use confirmed endpoint metadata. If there is no SSH metadata, use an unambiguous default-route source on that host. Do not guess from the first interface, dump the environment, scan networks, or use public IP discovery.
- **Map the documented endpoint to the observer.** Use the documented local URL only when they share the relevant context. Resolve host placeholders to the confirmed deployment IP/DNS; advertised backend or media addresses must be reachable by their consumers. For remote `localhost`, prefer an authorized, hostname-preserving forward or an observer in the deployment context. Translate to the host IP only for a service already published there, preserving scheme, published port, and path. Loopback-only services require forwarding. Mapping is validator preparation, not a change to the app or a waiver of documented configuration requirements.
- **Verify the route.** Use an unprivileged loopback listener reachable from the observer, with normal SSH host-key checks and no agent forwarding. Verify the assigned port, TLS identity, redirects, and WebSockets as applicable. Do not change application ports, firewall, or SSH settings. Probe only the expected endpoint; once running, confirm the observer reaches the same instance, for example by instance identity or correlated logs. Terminal access does not prove browser access. For WebRTC, confirm the documented ICE/media route separately: an HTTP(S) tunnel carries signaling, not the media path. Do not add services or rewrite media settings.

## 3. Verify TLS when applicable

- Separate connection, TLS, HTTP, and functional output. Follow valid documented trust handling. Otherwise compare the presented certificate's SHA-256 fingerprint with the **public** certificate obtained independently from the confirmed deployment; never read private keys. Check validity, chain, and hostname/IP SAN; CN alone does not establish a SAN match. Prefer isolated trust in the actual observer when identity matches. Terminal trust does not configure browser trust.
- For an authorized test deployment with a matching independent fingerprint and a valid certificate, a session-scoped browser exception may be used for diagnostics, including a known deployment-generated localhost certificate with a missing or mismatched SAN. Use only visible, supported controls in the selected isolated observer; verify that navigation succeeded. Do not use global TLS-disable flags, hidden bypasses, shared trust changes, or submit credentials through the TLS exception. If a browser cannot expose the controls or isolate trust, do not claim success or ask the user to click through; apply browser selection and readiness-stop as appropriate.
- Distinguish a trust error observed at the **original documented endpoint** from a hostname or route error introduced by the validator. Record the latter in Execution Notes; it is not an application defect or evidence of PASS or FAIL for the untested original endpoint.

## 4. Record and clean up

Record one mapping from deployment endpoint/output location through documented host values to observer URL/path and any forward. Reuse it and record changes, probes, deferred checks, and capture timing. Use `report-format.md` for TLS evidence. On completion or early stop, close only validator-created forwards and observers and remove only scoped trust state; record cleanup and any limitations. Leave shared connections, trust stores, and browser state unchanged.
