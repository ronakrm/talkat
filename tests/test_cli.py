"""Tests for talkat.cli — argparse dispatch and helper functions.

We don't actually record audio or hit a model server here. Each subcommand
test monkey-patches the underlying handler (run_dictation,
process_audio_file_command, etc.) and asserts main()'s dispatch routed to it
with the right arguments. Helpers (_overrides_from_args) are tested by
direct calls with ProcessManager methods stubbed.
"""

from __future__ import annotations

import argparse
import sys

import pytest

# ---------------------------------------------------------------------------
# _overrides_from_args
# ---------------------------------------------------------------------------


def test_overrides_from_args_filters_none_values():
    from talkat.cli import _overrides_from_args

    ns = argparse.Namespace(max_recording=None, http_timeout=None, language=None)
    assert _overrides_from_args(ns) == {}


def test_overrides_from_args_keys_match_config_names():
    from talkat.cli import _overrides_from_args

    ns = argparse.Namespace(max_recording=45.0, http_timeout=90.0, language="es")
    overrides = _overrides_from_args(ns)
    assert overrides == {
        "max_recording_duration": 45.0,
        "http_timeout": 90.0,
        "language": "es",
    }


def test_overrides_from_args_includes_only_set_values():
    from talkat.cli import _overrides_from_args

    ns = argparse.Namespace(max_recording=30.0, http_timeout=None, language=None)
    overrides = _overrides_from_args(ns)
    assert overrides == {"max_recording_duration": 30.0}


def test_overrides_from_args_handles_missing_attributes():
    """If an arg attribute is missing entirely (other subcommand) it's treated as None."""
    from talkat.cli import _overrides_from_args

    ns = argparse.Namespace()  # none of the override attributes exist
    assert _overrides_from_args(ns) == {}


# ---------------------------------------------------------------------------
# main() — argparse dispatch
# ---------------------------------------------------------------------------


def _run_main(argv: list[str]) -> int:
    """Run cli.main() with the given argv; return the SystemExit code (0 if no exit)."""
    from talkat.cli import main as cli_main

    try:
        cli_main()
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0
    return 0


def test_main_no_subcommand_prints_help_and_exits_one(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(sys, "argv", ["talkat"])
    rc = _run_main(["talkat"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Talkat" in out or "usage" in out.lower()


def test_main_calibrate_dispatches_to_run_calibrate(monkeypatch: pytest.MonkeyPatch):
    from talkat import main as main_mod

    calls: list = []
    monkeypatch.setattr(main_mod, "run_calibrate", lambda: calls.append("ran") or 0)
    monkeypatch.setattr(sys, "argv", ["talkat", "calibrate"])

    rc = _run_main(["talkat", "calibrate"])
    assert rc == 0
    assert calls == ["ran"]


def test_main_file_dispatches_to_process_audio_file_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    captured: dict = {}

    def fake_handler(
        file_path: str,
        output_file: str | None = None,
        output_format: str = "text",
        clipboard: bool = False,
        language: str | None = None,
        postprocess: str | None = None,
    ) -> int:
        captured["file"] = file_path
        captured["output"] = output_file
        captured["format"] = output_format
        captured["clipboard"] = clipboard
        captured["language"] = language
        captured["postprocess"] = postprocess
        return 0

    monkeypatch.setattr("talkat.file_processor.process_audio_file_command", fake_handler)

    src = tmp_path / "in.wav"
    src.write_bytes(b"\x00")
    out = tmp_path / "out.json"
    monkeypatch.setattr(
        sys, "argv", ["talkat", "file", str(src), "-o", str(out), "-f", "json", "-c"]
    )

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured == {
        "file": str(src),
        "output": str(out),
        "format": "json",
        "clipboard": True,
        "language": None,
        "postprocess": None,
    }


def test_main_batch_dispatches_to_batch_process_files(monkeypatch: pytest.MonkeyPatch, tmp_path):
    captured: dict = {}

    def fake_handler(
        files: list[str],
        output_dir: str | None = None,
        output_format: str = "text",
        language: str | None = None,
        postprocess: str | None = None,
    ) -> int:
        captured["files"] = files
        captured["dir"] = output_dir
        captured["format"] = output_format
        captured["language"] = language
        captured["postprocess"] = postprocess
        return 0

    monkeypatch.setattr("talkat.file_processor.batch_process_files", fake_handler)

    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    for f in (a, b):
        f.write_bytes(b"\x00")
    monkeypatch.setattr(
        sys, "argv", ["talkat", "batch", str(a), str(b), "-o", str(tmp_path), "-f", "srt"]
    )

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured["files"] == [str(a), str(b)]
    assert captured["dir"] == str(tmp_path)
    assert captured["format"] == "srt"
    assert captured["language"] is None
    assert captured["postprocess"] is None


def test_main_server_dispatches_to_model_server_main(monkeypatch: pytest.MonkeyPatch):
    """server subcommand must invoke model_server.main (we stub it to avoid the real loop)."""
    from talkat import model_server as ms

    calls: list = []
    monkeypatch.setattr(ms, "main", lambda: calls.append("ran"))
    monkeypatch.setattr(sys, "argv", ["talkat", "server"])

    # server command doesn't sys.exit; main() returns normally.
    _run_main(sys.argv)
    assert calls == ["ran"]


def test_main_install_service_dispatches(monkeypatch: pytest.MonkeyPatch):
    from talkat import service as svc

    monkeypatch.setattr(svc, "install_service", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["talkat", "install-service"])

    assert _run_main(sys.argv) == 0


def test_main_uninstall_service_dispatches(monkeypatch: pytest.MonkeyPatch):
    from talkat import service as svc

    monkeypatch.setattr(svc, "uninstall_service", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["talkat", "uninstall-service"])

    assert _run_main(sys.argv) == 0


def test_main_listen_dispatches_to_run_dictation_with_overrides(
    monkeypatch: pytest.MonkeyPatch, clean_pid_files
):
    """listen routes to run_dictation and passes its flags + overrides through."""
    from talkat import main as main_mod
    from talkat.process_manager import ProcessManager

    captured: dict = {}

    def fake_run_dictation(
        output_file: str | None = None,
        to_file: bool = False,
        config_overrides: dict | None = None,
        postprocess: str | None = None,
    ) -> int:
        captured["to_file"] = to_file
        captured["output_file"] = output_file
        captured["overrides"] = config_overrides
        captured["postprocess"] = postprocess
        return 0

    monkeypatch.setattr(main_mod, "run_dictation", fake_run_dictation)
    # Defang the lock + PID write so we don't need real microphone setup.
    monkeypatch.setattr(ProcessManager, "is_running", lambda _self: (False, None))
    monkeypatch.setattr(ProcessManager, "write_pid", lambda _self, _pid: None)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "talkat",
            "listen",
            "--to-file",
            "-o",
            "/tmp/out.txt",
            "--max-recording",
            "20",
            "--http-timeout",
            "45",
        ],
    )

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured["output_file"] == "/tmp/out.txt"
    assert captured["to_file"] is True
    assert captured["overrides"] == {
        "max_recording_duration": 20.0,
        "http_timeout": 45.0,
    }


def test_main_listen_defaults_to_typing(monkeypatch: pytest.MonkeyPatch, clean_pid_files):
    from talkat import main as main_mod
    from talkat.process_manager import ProcessManager

    captured: dict = {}
    monkeypatch.setattr(
        main_mod,
        "run_dictation",
        lambda output_file=None, to_file=False, **_kw: captured.update(
            output_file=output_file, to_file=to_file
        )
        or 0,
    )
    monkeypatch.setattr(ProcessManager, "is_running", lambda _self: (False, None))
    monkeypatch.setattr(ProcessManager, "write_pid", lambda _self, _pid: None)
    monkeypatch.setattr(sys, "argv", ["talkat", "listen"])

    assert _run_main(sys.argv) == 0
    assert captured == {"output_file": None, "to_file": False}


def test_main_listen_with_try_lock_refuses_when_already_running(
    monkeypatch: pytest.MonkeyPatch, clean_pid_files
):
    """`listen --try-lock` must exit 1 if another listen process is already active."""
    from talkat.process_manager import ProcessManager

    monkeypatch.setattr(ProcessManager, "is_running", lambda _self: (True, 9999))
    monkeypatch.setattr(sys, "argv", ["talkat", "listen", "--try-lock"])

    assert _run_main(sys.argv) == 1


# ---------------------------------------------------------------------------
# `talkat model` subcommand dispatch (§5c)
# ---------------------------------------------------------------------------


def test_main_model_no_subcommand_prints_help_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
):
    """Bare ``talkat model`` should NOT silently succeed — should explain usage."""
    monkeypatch.setattr(sys, "argv", ["talkat", "model"])
    rc = _run_main(["talkat", "model"])
    assert rc == 1


def test_main_model_list_invokes_list_models(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path):
    """``talkat model list`` calls list_models and prints a row per model."""
    from pathlib import Path

    from talkat import cli as cli_mod
    from talkat.model_manager import InstalledModel

    monkeypatch.setattr(
        cli_mod,
        "_run_model_command",
        cli_mod._run_model_command,  # use the real one; stub list_models below
    )

    import talkat.model_manager as mm

    fake_models = [
        InstalledModel(name="small.en", path=Path("/x/small.en"), size_bytes=500_000_000),
        InstalledModel(name="tiny.en", path=Path("/x/tiny.en"), size_bytes=75_000_000),
    ]
    monkeypatch.setattr(mm, "list_models", lambda config=None: fake_models)

    monkeypatch.setattr(sys, "argv", ["talkat", "model", "list"])
    rc = _run_main(sys.argv)
    assert rc == 0

    out = capsys.readouterr().out
    assert "small.en" in out
    assert "tiny.en" in out
    assert "NAME" in out  # header row


def test_main_model_list_handles_empty_cache(monkeypatch: pytest.MonkeyPatch, capsys):
    import talkat.model_manager as mm

    monkeypatch.setattr(mm, "list_models", lambda config=None: [])

    monkeypatch.setattr(sys, "argv", ["talkat", "model", "list"])
    rc = _run_main(sys.argv)
    assert rc == 0  # empty is not an error
    # No table header should appear when there's nothing to list.
    out = capsys.readouterr().out
    assert "NAME" not in out


def test_main_model_download_invokes_download_model(monkeypatch: pytest.MonkeyPatch):
    from pathlib import Path

    import talkat.model_manager as mm

    calls: dict[str, str] = {}

    def fake_download(name: str, config: dict | None = None) -> Path:
        calls["name"] = name
        return Path("/cache/fake-snapshot")

    monkeypatch.setattr(mm, "download_model", fake_download)
    monkeypatch.setattr(sys, "argv", ["talkat", "model", "download", "small.en"])

    rc = _run_main(sys.argv)
    assert rc == 0
    assert calls["name"] == "small.en"


def test_main_model_download_error_exits_one(monkeypatch: pytest.MonkeyPatch):
    import talkat.model_manager as mm

    def fake_download(name: str, config: dict | None = None):
        raise mm.ModelManagerError("network down")

    monkeypatch.setattr(mm, "download_model", fake_download)
    monkeypatch.setattr(sys, "argv", ["talkat", "model", "download", "small.en"])
    rc = _run_main(sys.argv)
    assert rc == 1


def test_main_model_use_invokes_use_model(monkeypatch: pytest.MonkeyPatch):
    from pathlib import Path

    import talkat.model_manager as mm

    calls: dict[str, str] = {}

    def fake_use(name: str) -> tuple[Path, bool]:
        calls["name"] = name
        return Path("/cfg/config.json"), True

    monkeypatch.setattr(mm, "use_model", fake_use)
    monkeypatch.setattr(sys, "argv", ["talkat", "model", "use", "tiny.en"])
    rc = _run_main(sys.argv)
    assert rc == 0
    assert calls["name"] == "tiny.en"


def test_main_model_use_rejects_unknown(monkeypatch: pytest.MonkeyPatch):
    import talkat.model_manager as mm

    def fake_use(name: str):
        raise mm.ModelManagerError(f"Unknown model name: {name!r}")

    monkeypatch.setattr(mm, "use_model", fake_use)
    monkeypatch.setattr(sys, "argv", ["talkat", "model", "use", "nonsense.zz"])
    rc = _run_main(sys.argv)
    assert rc == 1


# ---------------------------------------------------------------------------
# §5a --postprocess flag parsing (listen / file / batch)
# ---------------------------------------------------------------------------


def test_main_listen_passes_postprocess_through(monkeypatch: pytest.MonkeyPatch, clean_pid_files):
    from talkat import main as main_mod
    from talkat.process_manager import ProcessManager

    captured: dict = {}

    def fake_run_dictation(
        output_file: str | None = None,
        to_file: bool = False,
        config_overrides: dict | None = None,
        postprocess: str | None = None,
    ) -> int:
        captured["to_file"] = to_file
        captured["postprocess"] = postprocess
        return 0

    monkeypatch.setattr(main_mod, "run_dictation", fake_run_dictation)
    monkeypatch.setattr(ProcessManager, "is_running", lambda _self: (False, None))
    monkeypatch.setattr(ProcessManager, "write_pid", lambda _self, _pid: None)
    monkeypatch.setattr(sys, "argv", ["talkat", "listen", "--postprocess", "tidy"])

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured["postprocess"] == "tidy"


def test_main_file_passes_postprocess_through(monkeypatch: pytest.MonkeyPatch, tmp_path):
    captured: dict = {}

    def fake_handler(
        file_path: str,
        output_file: str | None = None,
        output_format: str = "text",
        clipboard: bool = False,
        language: str | None = None,
        postprocess: str | None = None,
    ) -> int:
        captured["postprocess"] = postprocess
        return 0

    monkeypatch.setattr("talkat.file_processor.process_audio_file_command", fake_handler)
    src = tmp_path / "in.wav"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(sys, "argv", ["talkat", "file", str(src), "--postprocess", "code"])

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured["postprocess"] == "code"


def test_main_batch_passes_postprocess_through(monkeypatch: pytest.MonkeyPatch, tmp_path):
    captured: dict = {}

    def fake_handler(
        files: list[str],
        output_dir: str | None = None,
        output_format: str = "text",
        language: str | None = None,
        postprocess: str | None = None,
    ) -> int:
        captured["postprocess"] = postprocess
        return 0

    monkeypatch.setattr("talkat.file_processor.batch_process_files", fake_handler)
    a = tmp_path / "a.wav"
    a.write_bytes(b"\x00")
    monkeypatch.setattr(sys, "argv", ["talkat", "batch", str(a), "--postprocess", "email"])

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured["postprocess"] == "email"


def test_main_listen_omits_postprocess_when_not_provided(
    monkeypatch: pytest.MonkeyPatch, clean_pid_files
):
    """No --postprocess flag → handler sees postprocess=None."""
    from talkat import main as main_mod
    from talkat.process_manager import ProcessManager

    captured: dict = {}

    def fake_run_dictation(
        output_file: str | None = None,
        to_file: bool = False,
        config_overrides: dict | None = None,
        postprocess: str | None = None,
    ) -> int:
        captured["to_file"] = to_file
        captured["postprocess"] = postprocess
        return 0

    monkeypatch.setattr(main_mod, "run_dictation", fake_run_dictation)
    monkeypatch.setattr(ProcessManager, "is_running", lambda _self: (False, None))
    monkeypatch.setattr(ProcessManager, "write_pid", lambda _self, _pid: None)
    monkeypatch.setattr(sys, "argv", ["talkat", "listen"])

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured["postprocess"] is None
