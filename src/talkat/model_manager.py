"""faster-whisper model management — list / download / use.

faster-whisper resolves names like ``small.en`` to a HuggingFace repo id
(``Systran/faster-whisper-small.en``) inside ``WhisperModel.__init__``.
``huggingface_hub.snapshot_download`` puts the resulting files into a
content-addressed directory ``models--<org>--<repo>/`` inside the cache
root. This module is just a thin layer over that cache layout — list what
the directory contains, download via the same path the constructor would,
and update the config so the next server start picks the new model.

Vosk isn't covered here. Vosk distributes models as zip files under
``https://alphacephei.com/vosk/models/`` rather than HuggingFace, and the
on-disk layout is one directory per language with no shared blob store.
That's enough of a separate feature to defer to a follow-up; for v1.0.0
we ship the faster-whisper path only (faster-whisper is the default
backend, and Whisper-size-juggling is the actual user pain point).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .logging_config import get_logger

logger = get_logger(__name__)


class ModelManagerError(RuntimeError):
    """Raised when model management can't complete (network, missing dep, etc.)."""


@dataclass(frozen=True)
class InstalledModel:
    """One row of ``talkat model list`` output."""

    name: str
    path: Path
    size_bytes: int


# HF cache layout: faster-whisper resolves "small.en" → "Systran/faster-whisper-small.en",
# then huggingface_hub stores it at "<cache>/models--Systran--faster-whisper-small.en/".
# We reverse-encode the directory name to display the friendly Whisper name.
_HF_PREFIX = "models--"
_FW_REPO_PREFIXES = ("faster-whisper-", "faster-distil-whisper-")


def _decode_hf_dirname(dirname: str) -> str | None:
    """Turn ``"models--Systran--faster-whisper-small.en"`` into ``"small.en"``.

    Returns None for unrecognized layouts (other repos, junk dirs). This means
    a model downloaded outside the faster-whisper family is silently skipped
    by ``list_models`` — that's intentional, since we can't safely promote
    such a directory to a friendly name.
    """
    if not dirname.startswith(_HF_PREFIX):
        return None
    parts = dirname[len(_HF_PREFIX) :].split("--")
    if len(parts) != 2:
        return None
    _org, repo = parts
    for prefix in _FW_REPO_PREFIXES:
        if repo.startswith(prefix):
            return repo[len(prefix) :]
    return None


def _dir_size_bytes(path: Path) -> int:
    """Sum every regular file under ``path`` once.

    HF caches store one canonical copy under ``blobs/`` and link snapshots
    to it. We skip symlinks during the walk so blobs aren't double-counted
    (once as the file, once as the snapshot symlink target).
    """
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            p = Path(root) / f
            try:
                if p.is_symlink():
                    continue
                total += p.stat().st_size
            except OSError:
                continue
    return total


def _resolve_cache_dir(config: dict[str, Any] | None = None) -> Path:
    from .config import CODE_DEFAULTS, load_app_config

    cfg = config if config is not None else load_app_config()
    cache = cfg.get(
        "faster_whisper_model_cache_dir",
        CODE_DEFAULTS["faster_whisper_model_cache_dir"],
    )
    return Path(os.path.expanduser(str(cache)))


def _resolve_repo_id(name: str) -> str:
    """Map a Whisper name like ``"small.en"`` to its HF repo id.

    Reuses faster-whisper's own ``_MODELS`` table — it's the de facto resolver
    the ``WhisperModel`` constructor uses, and has been stable since v0.6.
    Falls back to passing explicit ``org/repo`` names through unchanged so
    users can pull community-quantized variants by id.
    """
    try:
        from faster_whisper.utils import _MODELS
    except ImportError as e:
        raise ModelManagerError(
            "faster-whisper is not installed; cannot resolve model name. Install with: uv sync"
        ) from e

    if name in _MODELS:
        return str(_MODELS[name])
    if "/" in name:
        return name
    known = sorted(_MODELS)
    raise ModelManagerError(
        f"Unknown model name: {name!r}. "
        f"Known faster-whisper sizes: {', '.join(known)}. "
        "You can also pass an explicit HuggingFace repo id like 'org/repo-name'."
    )


def list_models(config: dict[str, Any] | None = None) -> list[InstalledModel]:
    """Return every faster-whisper model currently in the cache directory.

    Returns an empty list (not an error) when the cache dir is missing —
    fresh installs land here before any download.
    """
    cache_dir = _resolve_cache_dir(config)
    if not cache_dir.is_dir():
        return []

    out: list[InstalledModel] = []
    for entry in sorted(cache_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = _decode_hf_dirname(entry.name)
        if name is None:
            continue
        out.append(InstalledModel(name=name, path=entry, size_bytes=_dir_size_bytes(entry)))
    return out


def download_model(name: str, config: dict[str, Any] | None = None) -> Path:
    """Download a faster-whisper model into the cache directory.

    Idempotent: re-running for an already-cached model is a no-op (the HF
    cache is content-addressed). Returns the snapshot path on disk.

    Raises ``ModelManagerError`` on:
      - unknown model name (with the known-list error from _resolve_repo_id)
      - network / HF Hub failures
      - missing huggingface_hub (shouldn't happen — it's a faster-whisper dep)
    """
    from .security import validate_model_name

    name = validate_model_name(name)
    repo_id = _resolve_repo_id(name)
    cache_dir = _resolve_cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise ModelManagerError(
            "huggingface_hub is not installed; cannot download models. "
            "It should be pulled in by faster-whisper — try: uv sync"
        ) from e

    logger.info(f"Downloading {repo_id} into {cache_dir} (this can take a while)...")
    try:
        path = snapshot_download(repo_id=repo_id, cache_dir=str(cache_dir))
    except Exception as e:
        # huggingface_hub raises a handful of HfHub* exceptions; we don't want
        # the CLI surface to depend on its private exception hierarchy, so
        # collapse to a single typed error with the original chained.
        raise ModelManagerError(f"Failed to download {repo_id}: {e}") from e

    return Path(path)


def use_model(name: str) -> tuple[Path, bool]:
    """Set ``name`` as the default ``model_name`` in user config.

    Returns ``(config_file_path, was_already_cached)``. Does NOT download —
    call :func:`download_model` first if you want the model available right
    now. We log a warning when the chosen model isn't cached yet so users
    aren't surprised by a download on next server start.

    Raises ``ModelManagerError`` if the name isn't a known faster-whisper
    size and doesn't look like an HF repo id — reuses ``_resolve_repo_id``
    so the validity rule lives in exactly one place.
    """
    from .config import load_app_config, save_app_config
    from .paths import CONFIG_FILE
    from .security import validate_model_name

    name = validate_model_name(name)
    # Rejects ``nonsense.zz`` (not in _MODELS, no '/') with the same friendly
    # error ``download`` would give; accepts ``small.en`` and ``org/repo``.
    _resolve_repo_id(name)

    config = load_app_config()
    config["model_name"] = name
    save_app_config(config)

    installed_names = {m.name for m in list_models(config)}
    cached = name in installed_names
    if not cached:
        logger.warning(
            f"{name} is not yet downloaded. The model server will fetch it on next start "
            f"(or run: talkat model download {name})."
        )
    return CONFIG_FILE, cached


def known_model_names() -> list[str]:
    """Return faster-whisper's table of known model sizes, for CLI help text."""
    try:
        from faster_whisper.utils import _MODELS
    except ImportError:
        return []
    return sorted(_MODELS)


def format_size(num_bytes: int) -> str:
    """Format bytes as KB/MB/GB. Plain helper — keeps the CLI side stateless."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    units = ["KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PB"
