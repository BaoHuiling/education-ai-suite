import os
import sys

import yaml


def main():
    with open(os.environ["SMART_CLASSROOM_CONFIG_PATH"], encoding="utf-8-sig") as stream:
        config = yaml.safe_load(stream)

    asr = config["models"]["asr"]
    for key in ("provider", "name", "device"):
        value = asr.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"models.asr.{key} must be a non-empty string")

    for key in (
        "provider", "name", "device", "temperature", "diarization",
        "no_speech_threshold", "logprob_threshold", "min_duration_sec", "min_words",
    ):
        if key in asr and asr[key] is not None:
            os.environ[f"AUDIO_ANALYZER__MODELS__ASR__{key.upper()}"] = str(asr[key])

    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()