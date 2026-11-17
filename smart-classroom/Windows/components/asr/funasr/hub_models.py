# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""FunASR snapshots and the hubs that publish them.

The Hugging Face entries are pinned to commits whose files are byte-identical
to the ModelScope snapshots. ASR and punctuation have no such repo on
Hugging Face (``funasr/paraformer-zh`` and ``funasr/ct-punc`` are different
models), so they are fetched from ModelScope even with model_hub: huggingface.
"""

from utils.model_download_helper import HubModel

FUNASR_MODEL_MAP = {
    "paraformer-zh": "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "paraformer-en": "iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020",
    "paraformer-online": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",
}

# Shared by every ASR model and by CAM++ diarization.
VAD = HubModel(
    ms=("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", "v2.0.4"),
    hf=("funasr/fsmn-vad", "df20e6b30c653645fa4ff125cacfcabd1020a669"),
)
PUNC = HubModel(
    ms=("iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch", "v2.0.4"),
)
CAMPPLUS = HubModel(
    ms=("iic/speech_campplus_sv_zh-cn_16k-common", "v2.0.2"),
    hf=("funasr/campplus", "e4b6ede7ce16997aff4ae69fbca1f0175e2afede"),
    required_files=("config.yaml", "configuration.json", "campplus_cn_common.bin"),
)


def asr_model(model_name: str, revision=None) -> HubModel:
    if model_name not in FUNASR_MODEL_MAP:
        raise ValueError(f"Invalid ASR model name {model_name}. "
                         f"Supported models are: {list(FUNASR_MODEL_MAP.keys())}")
    return HubModel(ms=(FUNASR_MODEL_MAP[model_name], revision))
