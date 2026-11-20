"""
Legacy hardware wake-word connector for ATA.

A Windows voice-activation app logs each hardware keyword detection as:

    [2026:10:06 15:25:07.592] :: SignalDetected -- Signal detected: Cortana

This bridge tails that log and, on each NEW detection of the configured
keyword, triggers a normal ATA voice session via kiosk-core - exactly what the
mic button or the software wake word does. The ASR -> RAG -> LLM -> TTS pipeline
is unchanged.

Options:
    --log-path         hardware wake event log (else auto-detected)
    --keyword          keyword to react to (default: Cortana)
    --base-url         kiosk-core base URL (default: http://127.0.0.1:8012)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

# Matches: ":: SignalDetected -- Signal detected: Cortana"
_DETECT_RE = re.compile(r"SignalDetected\s*--\s*Signal detected:\s*(?P<kw>.+?)\s*$")

# Legacy voice agent package family name (its detection log lives under LocalState).
_VOICE_AGENT_PFN = "c230944b-3da9-44d1-ac7c-1bb3e3eb3ed3_mcm29a8d0gv8r"


def default_log_path() -> str:
    return os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Packages", _VOICE_AGENT_PFN, "LocalState", "MvaApplicationOne_logs.txt",
    )


def trigger_ata(base_url: str) -> bool:
    """Start a normal ATA session (same payload the software path sends)."""
    payload = {
        "sample_rate": 16000,
        "chunk_seconds": 4.0,
        "silence_timeout_seconds": 1.5,
        "max_session_seconds": 20.0,
        "language": "en",
        "tts_model": "kokoro",
        "tts_language": "English",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/v1/sessions/start", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[bridge] kiosk-core -> {resp.status}", flush=True)
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001 - surface any transport error to the user
        print(f"[bridge] failed to reach kiosk-core at {base_url}: {exc}", flush=True)
        print("[bridge] is start_ata.ps1 running?", flush=True)
        return False


def wait_for_log(path: str) -> None:
    if os.path.exists(path):
        return
    print("[bridge] waiting for hardware wake event log", flush=True)
    while not os.path.exists(path):
        time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(prog="hardware-wake-bridge", description="ATA hardware wake-word bridge (hardware event log).")
    parser.add_argument("--log-path", default=os.environ.get("MVA_LOG_PATH") or default_log_path())
    parser.add_argument("--keyword", default=os.environ.get("MVA_WAKE_KEYWORD", "Cortana"))
    parser.add_argument("--base-url", default=os.environ.get("KIOSK_CORE_UI_BASE_URL", "http://127.0.0.1:8012"))
    parser.add_argument("--cooldown-seconds", type=float, default=3.0,
                        help="Ignore repeat detections within this window")
    parser.add_argument("--poll-interval", type=float, default=0.25)
    args = parser.parse_args()

    keyword = args.keyword.strip().lower()
    print(f"[bridge] watching hardware wake event log for keyword '{args.keyword}'", flush=True)
    wait_for_log(args.log_path)

    # Start at end of file so only NEW detections (after we start) trigger ATA.
    position = os.path.getsize(args.log_path)
    last_trigger = 0.0

    while True:
        try:
            size = os.path.getsize(args.log_path)
            if size < position:  # log rotated/truncated - restart from the top
                position = 0
            if size > position:
                with open(args.log_path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(position)
                    new = fh.read()
                    position = fh.tell()
                for line in new.splitlines():
                    m = _DETECT_RE.search(line)
                    if not m:
                        continue
                    detected = m.group("kw").strip().lower()
                    if detected != keyword:
                        continue
                    now = time.monotonic()
                    if now - last_trigger < args.cooldown_seconds:
                        continue
                    last_trigger = now
                    print(f"[bridge] '{args.keyword}' detected -> triggering ATA", flush=True)
                    trigger_ata(args.base_url)
            time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            print("\n[bridge] stopped.", flush=True)
            return 0
        except Exception as exc:  # noqa: BLE001 - keep tailing despite transient IO errors
            print(f"[bridge] warning: {exc}", flush=True)
            time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
