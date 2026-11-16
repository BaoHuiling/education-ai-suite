# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""models.model_hub reaches every FunASR download, and ModelScope failures say what to do."""

import importlib
import os
import sys

import huggingface_hub
import pytest

# The package re-exports a function of the same name, which shadows the module
# for ``import ... as``; the helper imports the function from this module.
ms_download = importlib.import_module("modelscope.hub.snapshot_download")

_SC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)

from components.asr.funasr import hub_models
from utils import model_download_helper as helper
from utils import model_hub
from utils.ensure_model import ModelDownloadError

class _Calls(list):
    """(hub, repo, revision) per download, plus the remaining kwargs in ``.kwargs``."""

    def __init__(self):
        super().__init__()
        self.kwargs = []


RATE_LIMITED = RuntimeError("[E3002] [403] 操作过于频繁，请稍后再试! | code=10013201001")


@pytest.fixture
def downloads(monkeypatch):
    """Record snapshot_download calls per hub; write the required files like a real one."""
    calls = _Calls()
    calls_kwargs = calls.kwargs

    def fake(hub):
        def download(repo_id, revision=None, local_dir=None, **kwargs):
            calls.append((hub, repo_id, revision))
            calls_kwargs.append(kwargs)
            os.makedirs(local_dir, exist_ok=True)
            for name in ("config.yaml", "configuration.json", "model.pt",
                         "campplus_cn_common.bin"):
                open(os.path.join(local_dir, name), "w").close()
            return local_dir
        return download

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake("hf"))
    monkeypatch.setattr(ms_download, "snapshot_download", fake("ms"))
    return calls


def _hub(monkeypatch, hub):
    monkeypatch.setattr(helper, "get_model_hub", lambda: hub)
    monkeypatch.setattr(model_hub, "get_model_hub", lambda: hub)


@pytest.mark.parametrize("value, hub", [
    ("huggingface", "hf"), ("HuggingFace", "hf"), ("hf", "hf"), (None, "hf"), ("", "hf"),
    ("modelscope", "ms"), (" ms ", "ms"),
])
def test_config_spellings_map_to_one_hub(value, hub):
    assert model_hub.normalize_hub(value) == hub


def test_an_unknown_hub_is_rejected():
    with pytest.raises(ValueError, match="huggingface or modelscope"):
        model_hub.normalize_hub("hugginface")


def test_hub_env_reaches_paddle_and_the_hf_mirror(monkeypatch):
    monkeypatch.delenv("PADDLE_PDX_MODEL_SOURCE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    model_hub.apply_hub_env({"model_hub": "modelscope", "hf_endpoint": "https://hf-mirror.com/"})
    assert os.environ["PADDLE_PDX_MODEL_SOURCE"] == "modelscope"
    assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"


def test_variables_already_in_the_environment_win(monkeypatch):
    monkeypatch.setenv("PADDLE_PDX_MODEL_SOURCE", "bos")
    monkeypatch.setenv("HF_ENDPOINT", "https://mine.example")
    model_hub.apply_hub_env({"model_hub": "huggingface", "hf_endpoint": "https://hf-mirror.com"})
    assert os.environ["PADDLE_PDX_MODEL_SOURCE"] == "bos"
    assert os.environ["HF_ENDPOINT"] == "https://mine.example"


def test_a_blank_mirror_leaves_hf_endpoint_unset(monkeypatch):
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    model_hub.apply_hub_env({"model_hub": "huggingface", "hf_endpoint": ""})
    assert "HF_ENDPOINT" not in os.environ


def test_huggingface_hub_moves_vad_and_campplus(tmp_path, monkeypatch, downloads):
    _hub(monkeypatch, "hf")
    helper.get_or_download_model_dir(hub_models.VAD, str(tmp_path / "vad"))
    helper.get_or_download_model_dir(hub_models.CAMPPLUS, str(tmp_path / "spk"))
    assert downloads == [("hf", *hub_models.VAD.hf), ("hf", *hub_models.CAMPPLUS.hf)]


def test_modelscope_only_models_ignore_huggingface_hub(tmp_path, monkeypatch, downloads):
    _hub(monkeypatch, "hf")
    helper.get_or_download_model_dir(hub_models.PUNC, str(tmp_path / "punc"))
    helper.get_or_download_model_dir(hub_models.asr_model("paraformer-zh"), str(tmp_path / "asr"))
    assert [hub for hub, _, _ in downloads] == ["ms", "ms"]


def test_modelscope_hub_keeps_every_model_on_modelscope(tmp_path, monkeypatch, downloads):
    _hub(monkeypatch, "ms")
    helper.get_or_download_model_dir(hub_models.VAD, str(tmp_path / "vad"))
    assert downloads == [("ms", *hub_models.VAD.ms)]


def test_a_complete_snapshot_is_reused_whatever_the_hub(tmp_path, monkeypatch, downloads):
    local = tmp_path / "vad"
    local.mkdir()
    for name in hub_models.VAD.required_files:
        (local / name).touch()
    for hub in ("hf", "ms"):
        _hub(monkeypatch, hub)
        assert helper.get_or_download_model_dir(hub_models.VAD, str(local)) == str(local.resolve())
    assert downloads == []


def test_an_interrupted_snapshot_is_downloaded_again(tmp_path, monkeypatch, downloads):
    """ModelScope leaves ._____temp behind when a download dies; that is not a model."""
    local = tmp_path / "punc"
    (local / "._____temp").mkdir(parents=True)
    (local / "config.yaml").touch()
    _hub(monkeypatch, "hf")
    helper.get_or_download_model_dir(hub_models.PUNC, str(local))
    assert downloads == [("ms", *hub_models.PUNC.ms)]


def test_modelscope_rate_limit_tells_the_user_what_to_do(tmp_path, monkeypatch):
    def throttled(*_, **__):
        raise RATE_LIMITED

    monkeypatch.setattr(ms_download, "snapshot_download", throttled)
    _hub(monkeypatch, "hf")
    with pytest.raises(ModelDownloadError) as err:
        helper.get_or_download_model_dir(hub_models.PUNC, str(tmp_path / "punc"))
    msg = str(err.value)
    assert hub_models.PUNC.name in msg
    assert "rate-limiting" in msg
    assert "only published on ModelScope" in msg
    assert f"modelscope download {hub_models.PUNC.name} --revision" in msg
    assert err.value.__cause__ is RATE_LIMITED


def test_modelscope_failure_points_at_huggingface_when_it_has_the_model(tmp_path, monkeypatch):
    def offline(*_, **__):
        raise ConnectionError("Max retries exceeded")

    monkeypatch.setattr(ms_download, "snapshot_download", offline)
    _hub(monkeypatch, "ms")
    with pytest.raises(ModelDownloadError) as err:
        helper.get_or_download_model_dir(hub_models.VAD, str(tmp_path / "vad"))
    msg = str(err.value)
    assert "modelscope.cn is reachable" in msg
    assert "models.model_hub: huggingface" in msg


def test_an_unknown_funasr_model_is_rejected():
    with pytest.raises(ValueError, match="paraformer-zh"):
        hub_models.asr_model("paraformer-xx")


# --- downloads that go elsewhere than models.model_hub say so, once they happen ---

def _mismatch_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelname == "WARNING" and "does not apply to it" in r.getMessage()]


def test_a_modelscope_only_download_warns_under_huggingface_hub(tmp_path, monkeypatch, downloads, caplog):
    _hub(monkeypatch, "hf")
    helper.get_or_download_model_dir(hub_models.PUNC, str(tmp_path / "punc"))
    assert _mismatch_warnings(caplog) == [
        f"'{hub_models.PUNC.name}' is downloaded from ModelScope only; "
        "models.model_hub: huggingface does not apply to it."
    ]


def test_downloads_on_the_configured_hub_do_not_warn(tmp_path, monkeypatch, downloads, caplog):
    _hub(monkeypatch, "hf")
    helper.get_or_download_model_dir(hub_models.VAD, str(tmp_path / "vad"))
    assert _mismatch_warnings(caplog) == []


def test_a_cached_modelscope_only_model_stays_quiet(tmp_path, monkeypatch, downloads, caplog):
    local = tmp_path / "punc"
    local.mkdir()
    for name in hub_models.PUNC.required_files:
        (local / name).touch()
    _hub(monkeypatch, "hf")
    helper.get_or_download_model_dir(hub_models.PUNC, str(local))
    assert downloads == [] and _mismatch_warnings(caplog) == []


def test_a_huggingface_only_export_warns_under_modelscope_hub(monkeypatch, caplog):
    _hub(monkeypatch, "ms")
    model_hub.warn_if_not_configured_hub("openai/whisper-base", model_hub.HF)
    assert _mismatch_warnings(caplog) == [
        "'openai/whisper-base' is downloaded from Hugging Face only; "
        "models.model_hub: modelscope does not apply to it."
    ]


# --- pyannote and pre-converted IR follow models.model_hub ---

@pytest.mark.parametrize("hub", ["hf", "ms"])
def test_pyannote_comes_from_the_configured_hub(tmp_path, monkeypatch, downloads, hub):
    from utils import ensure_model

    _hub(monkeypatch, hub)
    ensure_model._download_hub_model("pyannote/speaker-diarization-community-1",
                                     str(tmp_path / "pipe"), hf_token="hf_x",
                                     required_files=["config.yaml"])
    assert downloads == [(hub, "pyannote/speaker-diarization-community-1", None)]
    # The Hugging Face token never goes to ModelScope.
    assert downloads.kwargs[0].get("token") == ("hf_x" if hub == "hf" else None)


def test_a_pyannote_modelscope_failure_tells_the_user_what_to_do(tmp_path, monkeypatch):
    from utils import ensure_model

    def throttled(*_, **__):
        raise RATE_LIMITED

    monkeypatch.setattr(ms_download, "snapshot_download", throttled)
    _hub(monkeypatch, "ms")
    with pytest.raises(ModelDownloadError) as err:
        ensure_model._download_hub_model("pyannote/speaker-diarization-community-1",
                                         str(tmp_path / "pipe"), required_files=["config.yaml"])
    msg = str(err.value)
    assert "rate-limiting" in msg and "models.model_hub: huggingface" in msg


def test_the_gated_hint_offers_modelscope():
    from utils import ensure_model

    assert "models.model_hub: modelscope" in ensure_model._hf_auth_hint("pyannote/x")
