"""Tests for talkat.model_manager — list / download / use for faster-whisper.

We don't actually download from HuggingFace. The download tests stub
``huggingface_hub.snapshot_download`` so we exercise the call site, repo-id
resolution, and the error-wrapping contract without depending on network.

``list_models`` is tested against a synthetic on-disk layout that mimics
the real HF cache: ``models--<org>--<repo>/`` directories with blobs and a
symlinked snapshot — that's the layout that drives the `_dir_size_bytes`
symlink-skip behavior.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from talkat.model_manager import (
    InstalledModel,
    ModelManagerError,
    _decode_hf_dirname,
    _dir_size_bytes,
    _resolve_cache_dir,
    _resolve_repo_id,
    download_model,
    format_size,
    known_model_names,
    list_models,
    use_model,
)

# ---------------------------------------------------------------------------
# _decode_hf_dirname
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dirname,expected",
    [
        ("models--Systran--faster-whisper-small.en", "small.en"),
        ("models--Systran--faster-whisper-tiny", "tiny"),
        ("models--Systran--faster-whisper-large-v3", "large-v3"),
        ("models--Systran--faster-distil-whisper-large-v3", "large-v3"),
        ("models--Systran--faster-distil-whisper-medium.en", "medium.en"),
        ("models--mobiuslabsgmbh--faster-whisper-large-v3-turbo", "large-v3-turbo"),
    ],
)
def test_decode_hf_dirname_recognized(dirname: str, expected: str):
    assert _decode_hf_dirname(dirname) == expected


@pytest.mark.parametrize(
    "dirname",
    [
        "",
        "small.en",  # no models-- prefix
        "models-Systran-faster-whisper-small.en",  # single hyphens
        "models--Systran--unrelated-thing",  # not a faster-whisper repo
        "models--Systran",  # missing repo half
        "models--Systran--faster-whisper-small.en--extra",  # too many --
    ],
)
def test_decode_hf_dirname_rejects_unrecognized(dirname: str):
    assert _decode_hf_dirname(dirname) is None


# ---------------------------------------------------------------------------
# _dir_size_bytes
# ---------------------------------------------------------------------------


def test_dir_size_bytes_sums_regular_files(tmp_path: Path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"x" * 50)
    assert _dir_size_bytes(tmp_path) == 150


def test_dir_size_bytes_skips_symlinks(tmp_path: Path):
    """HF stores one canonical blob; snapshots are symlinks. Counting them
    both would double-report. This test enforces the skip-symlinks rule."""
    blob = tmp_path / "blobs" / "deadbeef"
    blob.parent.mkdir()
    blob.write_bytes(b"x" * 200)

    snapshot_dir = tmp_path / "snapshots" / "abc123"
    snapshot_dir.mkdir(parents=True)
    os.symlink(blob, snapshot_dir / "model.bin")

    # Real blob is 200B; symlink does NOT add 200 more.
    assert _dir_size_bytes(tmp_path) == 200


def test_dir_size_bytes_missing_path_returns_zero(tmp_path: Path):
    assert _dir_size_bytes(tmp_path / "nope") == 0


# ---------------------------------------------------------------------------
# _resolve_cache_dir
# ---------------------------------------------------------------------------


def test_resolve_cache_dir_uses_provided_config(tmp_path: Path):
    config = {"faster_whisper_model_cache_dir": str(tmp_path / "custom")}
    assert _resolve_cache_dir(config) == tmp_path / "custom"


def test_resolve_cache_dir_expands_user(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", "/home/example")
    config = {"faster_whisper_model_cache_dir": "~/elsewhere"}
    assert _resolve_cache_dir(config) == Path("/home/example/elsewhere")


# ---------------------------------------------------------------------------
# _resolve_repo_id
# ---------------------------------------------------------------------------


def test_resolve_repo_id_known_name():
    assert _resolve_repo_id("small.en") == "Systran/faster-whisper-small.en"


def test_resolve_repo_id_explicit_repo_id_passes_through():
    """Power users can pass org/repo for community-quantized variants."""
    assert _resolve_repo_id("mycorp/whisper-fine-tuned") == "mycorp/whisper-fine-tuned"


def test_resolve_repo_id_unknown_raises_with_known_list():
    with pytest.raises(ModelManagerError, match="Unknown model name"):
        _resolve_repo_id("nonsense.zz")


# ---------------------------------------------------------------------------
# format_size
# ---------------------------------------------------------------------------


def test_format_size_bytes():
    assert format_size(0) == "0 B"
    assert format_size(512) == "512 B"


def test_format_size_kb():
    assert format_size(2048) == "2.0 KB"


def test_format_size_mb():
    assert format_size(150 * 1024 * 1024) == "150.0 MB"


def test_format_size_gb():
    assert format_size(3 * 1024 * 1024 * 1024) == "3.0 GB"


# ---------------------------------------------------------------------------
# known_model_names
# ---------------------------------------------------------------------------


def test_known_model_names_is_sorted_and_includes_canonical():
    names = known_model_names()
    assert names == sorted(names)
    # These three are stable across faster-whisper versions — guard against
    # accidental future breakage if `_MODELS` ever drops a canonical size.
    for canon in ("tiny", "small.en", "large-v3"):
        assert canon in names


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


def _make_fake_model_dir(cache: Path, repo_dir: str, blob_size: int = 100) -> Path:
    """Create a minimal HF-cache-shaped directory under ``cache``.

    Layout matches what huggingface_hub actually produces:
        cache/repo_dir/blobs/<hash>
        cache/repo_dir/snapshots/<rev>/model.bin -> ../../blobs/<hash>
    """
    model_root = cache / repo_dir
    blobs = model_root / "blobs"
    blobs.mkdir(parents=True)
    blob = blobs / "deadbeef"
    blob.write_bytes(b"x" * blob_size)
    snapshot_dir = model_root / "snapshots" / "abc123"
    snapshot_dir.mkdir(parents=True)
    os.symlink(blob, snapshot_dir / "model.bin")
    return model_root


def test_list_models_empty_when_cache_dir_missing(tmp_path: Path):
    config = {"faster_whisper_model_cache_dir": str(tmp_path / "missing")}
    assert list_models(config) == []


def test_list_models_returns_installed_with_sizes(tmp_path: Path):
    _make_fake_model_dir(tmp_path, "models--Systran--faster-whisper-tiny.en", blob_size=100)
    _make_fake_model_dir(tmp_path, "models--Systran--faster-whisper-small.en", blob_size=500)

    config = {"faster_whisper_model_cache_dir": str(tmp_path)}
    models = list_models(config)

    assert [m.name for m in models] == ["small.en", "tiny.en"]  # sorted by dir name
    sizes = {m.name: m.size_bytes for m in models}
    assert sizes["tiny.en"] == 100
    assert sizes["small.en"] == 500


def test_list_models_ignores_unrecognized_dirs(tmp_path: Path):
    """Stray non-HF directories under the cache must not appear in output."""
    _make_fake_model_dir(tmp_path, "models--Systran--faster-whisper-base.en", blob_size=50)
    (tmp_path / "random-junk").mkdir()
    (tmp_path / "models--unrelated--something").mkdir()

    config = {"faster_whisper_model_cache_dir": str(tmp_path)}
    models = list_models(config)
    assert [m.name for m in models] == ["base.en"]


def test_list_models_ignores_loose_files(tmp_path: Path):
    """A stray file at the cache root must be skipped, not crash the walk."""
    (tmp_path / "stray.txt").write_text("hello")
    config = {"faster_whisper_model_cache_dir": str(tmp_path)}
    assert list_models(config) == []


# ---------------------------------------------------------------------------
# download_model — stubs huggingface_hub
# ---------------------------------------------------------------------------


def test_download_model_invokes_snapshot_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, object] = {}

    def fake_snapshot_download(repo_id: str, cache_dir: str) -> str:
        calls["repo_id"] = repo_id
        calls["cache_dir"] = cache_dir
        return str(tmp_path / "fake-snapshot")

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    config = {"faster_whisper_model_cache_dir": str(tmp_path / "cache")}
    path = download_model("small.en", config=config)

    assert calls["repo_id"] == "Systran/faster-whisper-small.en"
    assert calls["cache_dir"] == str(tmp_path / "cache")
    assert path == tmp_path / "fake-snapshot"
    # Cache dir should have been created.
    assert (tmp_path / "cache").is_dir()


def test_download_model_unknown_name_raises_before_calling_hf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Unknown names must fail validation BEFORE we hit the network."""
    called = False

    def fake_snapshot_download(repo_id: str, cache_dir: str) -> str:
        nonlocal called
        called = True
        return ""

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    config = {"faster_whisper_model_cache_dir": str(tmp_path)}

    with pytest.raises(ModelManagerError, match="Unknown model name"):
        download_model("nonsense.zz", config=config)
    assert called is False, "should have failed before reaching HF"


def test_download_model_wraps_hub_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A network / HF error must surface as ModelManagerError, not the raw
    HfHub* exception — keeps the CLI surface free of huggingface_hub's
    private exception hierarchy."""

    def fake_snapshot_download(repo_id: str, cache_dir: str) -> str:
        raise OSError("DNS unreachable")

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    config = {"faster_whisper_model_cache_dir": str(tmp_path)}

    with pytest.raises(ModelManagerError, match="Failed to download"):
        download_model("small.en", config=config)


def test_download_model_passes_through_explicit_repo_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: dict[str, str] = {}

    def fake_snapshot_download(repo_id: str, cache_dir: str) -> str:
        calls["repo_id"] = repo_id
        return str(tmp_path / "fake")

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    config = {"faster_whisper_model_cache_dir": str(tmp_path)}

    download_model("mycorp/whisper-finetune", config=config)
    assert calls["repo_id"] == "mycorp/whisper-finetune"


# ---------------------------------------------------------------------------
# use_model
# ---------------------------------------------------------------------------


def test_use_model_writes_model_name_to_config(clean_config_file):
    config_path, cached = use_model("tiny.en")
    assert config_path == clean_config_file

    from talkat.config import load_app_config

    cfg = load_app_config()
    assert cfg["model_name"] == "tiny.en"
    # No tiny.en in the test XDG_CACHE_HOME → cached=False is the expected signal.
    assert cached is False


def test_use_model_returns_cached_true_when_model_present(
    clean_config_file, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """When the named model is already in the cache, use_model() reports it.

    We set a per-call cache dir into the config + create the HF-shaped dir,
    then call use_model. It re-reads the config (which now points at our
    tmp cache) and sees the model is present.
    """
    cache = tmp_path / "fw-cache"
    _make_fake_model_dir(cache, "models--Systran--faster-whisper-small.en")

    # Seed the user config with our test cache dir so use_model's internal
    # list_models() sees the synthetic HF layout.
    from talkat.config import save_app_config

    save_app_config({"faster_whisper_model_cache_dir": str(cache)})

    _config_path, cached = use_model("small.en")
    assert cached is True


def test_use_model_rejects_unknown_name(clean_config_file):
    with pytest.raises(ModelManagerError, match="Unknown model name"):
        use_model("nonsense.zz")
    # Reject must happen BEFORE the config is written.
    from talkat.config import load_app_config

    cfg = load_app_config()
    assert cfg["model_name"] != "nonsense.zz"


def test_use_model_accepts_explicit_repo_id(clean_config_file):
    config_path, cached = use_model("mycorp/whisper-finetune")
    assert config_path == clean_config_file
    from talkat.config import load_app_config

    cfg = load_app_config()
    assert cfg["model_name"] == "mycorp/whisper-finetune"
    assert cached is False


# ---------------------------------------------------------------------------
# InstalledModel dataclass
# ---------------------------------------------------------------------------


def test_installed_model_is_immutable(tmp_path: Path):
    m = InstalledModel(name="small.en", path=tmp_path, size_bytes=100)
    with pytest.raises((AttributeError, Exception)):  # frozen dataclass
        m.name = "other"  # type: ignore[misc]
