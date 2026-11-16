import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from utils.ensure_model import ModelDownloadError
from utils.model_hub import HF, MS, get_model_hub, warn_if_not_configured_hub

logger = logging.getLogger(__name__)

# What ModelScope sends back when it throttles: HTTP 403 with this code/message.
_MS_RATE_LIMIT_MARKERS = ("10013201001", "操作过于频繁")


@dataclass(frozen=True)
class HubModel:
    """One model snapshot and where each hub publishes it.

    ``ms``/``hf`` are ``(repo_id, revision)``; a ``None`` revision means the
    hub's default branch. Pin ``hf`` to a commit whose files
    match the ModelScope snapshot byte for byte; leave it ``None`` when there is
    no such repo, and the model always comes from ModelScope.
    """

    ms: Tuple[str, Optional[str]]
    hf: Optional[Tuple[str, str]] = None
    # Present in a complete snapshot. An interrupted download leaves a
    # non-empty directory, so "has files" alone is not enough.
    required_files: Tuple[str, ...] = ("config.yaml", "configuration.json", "model.pt")

    @property
    def name(self) -> str:
        return self.ms[0]


def _is_complete(local_dir: str, required_files) -> bool:
    return all(os.path.isfile(os.path.join(local_dir, f)) for f in required_files)


def _resolve_hub(model: HubModel, hub: Optional[str]) -> str:
    hub = hub or get_model_hub()
    return hub if hub == MS or model.hf is not None else MS


def _ms_failure_message(repo: str, revision: Optional[str], local_dir: str,
                        error: Exception, on_hf: bool) -> str:
    at = f" (revision {revision})" if revision else ""
    lines = [f"Failed to download '{repo}'{at} from ModelScope into {local_dir}: {error}"]
    if any(marker in str(error) for marker in _MS_RATE_LIMIT_MARKERS):
        lines.append("ModelScope is rate-limiting this machine (HTTP 403). "
                     "Wait a few minutes, then restart the backend.")
    else:
        lines.append("Check that https://modelscope.cn is reachable (network/proxy).")
    if on_hf:
        lines.append("This model is also published on Hugging Face: set "
                     "models.model_hub: huggingface in config.yaml to download it from there.")
    else:
        lines.append("This model is only published on ModelScope, so changing "
                     "models.model_hub does not avoid this download.")
    rev_arg = f" --revision {revision}" if revision else ""
    lines.append(f"To fetch it by hand: modelscope download {repo}{rev_arg} "
                 f"--local-dir \"{local_dir}\"")
    return "\n".join(lines)


def download_snapshot(hub: str, repo: str, local_dir: str, *, revision: Optional[str] = None,
                      allow_patterns=None, hf_token: Optional[str] = None,
                      on_hf: bool = True) -> None:
    """Fetch one snapshot from ``hub`` into ``local_dir``.

    Warns when ``hub`` is not ``models.model_hub``. ModelScope failures become a
    ModelDownloadError that tells the user what to do; ``on_hf`` says whether
    switching to Hugging Face is one of the options. Hugging Face errors pass
    through unchanged for the caller to handle.
    """
    warn_if_not_configured_hub(repo, hub)
    at = revision or "default"
    if hub == HF:
        from huggingface_hub import snapshot_download

        logger.info(f"Downloading {repo}@{at} from Hugging Face -> {local_dir}")
        snapshot_download(repo_id=repo, revision=revision, local_dir=local_dir,
                          allow_patterns=allow_patterns, token=hf_token)
        return

    from modelscope.hub.snapshot_download import snapshot_download

    logger.info(f"Downloading {repo}@{at} from ModelScope -> {local_dir}")
    try:
        snapshot_download(repo, revision=revision, local_dir=local_dir,
                          allow_patterns=allow_patterns)
    except Exception as e:
        raise ModelDownloadError(
            _ms_failure_message(repo, revision, local_dir, e, on_hf)) from e


def get_or_download_model_dir(model: HubModel, local_dir: str, hub: Optional[str] = None) -> str:
    """Return ``local_dir`` holding a complete snapshot of ``model``, downloading if needed.

    ``hub`` overrides ``models.model_hub``. The directory is the same for both
    hubs, so switching hubs never re-downloads a model that is already there.
    """
    local_dir = os.path.normpath(local_dir)
    if _is_complete(local_dir, model.required_files):
        return str(Path(local_dir).resolve())
    if os.path.isdir(local_dir) and any(Path(local_dir).iterdir()):
        logger.warning(f"Incomplete model snapshot at {local_dir}; downloading again.")

    hub = _resolve_hub(model, hub)
    repo, revision = model.hf if hub == HF else model.ms
    download_snapshot(hub, repo, local_dir, revision=revision, on_hf=model.hf is not None)

    if not _is_complete(local_dir, model.required_files):
        missing = [f for f in model.required_files
                   if not os.path.isfile(os.path.join(local_dir, f))]
        raise ModelDownloadError(
            f"Downloaded '{model.name}' into {local_dir} but {', '.join(missing)} "
            "is missing; delete the directory and restart the backend."
        )
    return str(Path(local_dir).resolve())
