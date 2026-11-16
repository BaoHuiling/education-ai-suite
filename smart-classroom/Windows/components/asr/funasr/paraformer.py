from components.asr.base_asr import BaseASR
from utils import ensure_model
from utils.model_download_helper import get_or_download_model_dir
from components.asr.funasr import hub_models
from funasr import AutoModel

import os
import re
import logging
logger = logging.getLogger(__name__)


def _is_empty_input_error(msg: str) -> bool:
    return "shapes cannot be multiplied" in msg and bool(re.search(r"\(\d+x0\b", msg))


class Paraformer(BaseASR):
    def __init__(self, model_name, device="cpu", revision="v2.0.4"):
        asr = hub_models.asr_model(model_name, revision)
        model_name = asr.name
        model_dir = get_or_download_model_dir(asr, ensure_model.get_asr_model_path())

        # VAD and punctuation sit next to the ASR model, shared by every ASR model.
        model_dir_parent = os.path.dirname(model_dir)
        vad_model_dir = get_or_download_model_dir(
            hub_models.VAD, os.path.join(model_dir_parent, hub_models.VAD.name))
        punc_model_dir = get_or_download_model_dir(
            hub_models.PUNC, os.path.join(model_dir_parent, hub_models.PUNC.name))

        self.model_name = model_name
        self.model = AutoModel(model=model_dir, model_revision=revision,
                        vad_model=vad_model_dir, vad_model_revision="v2.0.4",
                        punc_model=punc_model_dir, punc_model_revision="v2.0.4",
                        #   spk_model="cam++", spk_model_revision="v2.0.2",
                        device=device, disable_update=True
                        )

    def transcribe(self, audio_path: str, temperature=0.0) -> dict:
        try:
            res = self.model.generate(
                input=audio_path,
                sentence_timestamp=True,
                batch_size_s=300
            )

            if not res:
                return {"text": "", "segments": []}

            out = res[0]

            segments = []
            if "sentence_info" in out:
                for s in out["sentence_info"]:
                    segments.append({
                        "start": s["start"] / 1000.0,  # ms → seconds
                        "end": s["end"] / 1000.0,
                        "text": s["text"].strip()
                    })

            return {
                "text": out["text"].strip(),
                "segments": segments
            }

        except Exception as e:
            if _is_empty_input_error(str(e)):
                logger.info(f"[ASR] empty/silent audio chunk skipped: {audio_path}")
                return {"text": "", "segments": []}
            # Raise like the other ASR backends: an empty result here would be
            # recorded as a successful model call and a finished transcription.
            logger.error(f"[ASR] Paraformer transcription error: {e}")
            raise