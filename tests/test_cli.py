"""Tests for talkat.cli — argparse dispatch and helper functions.

We don't actually record audio or hit a model server here. Each subcommand
test monkey-patches the underlying handler (listen_once, listen_continuous,
process_audio_file_command, etc.) and asserts main()'s dispatch routed to it
with the right arguments. Helper functions (_overrides_from_args,
_start_long, start_long_background, etc.) are tested by direct calls with
ProcessManager methods stubbed.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# _overrides_from_args
# ---------------------------------------------------------------------------


def test_overrides_from_args_filters_none_values():
    from talkat.cli import _overrides_from_args

    ns = argparse.Namespace(max_recording=None, silence_duration=None, http_timeout=None)
    assert _overrides_from_args(ns) == {}


def test_overrides_from_args_keys_match_config_names():
    from talkat.cli import _overrides_from_args

    ns = argparse.Namespace(max_recording=45.0, silence_duration=2.5, http_timeout=90.0)
    overrides = _overrides_from_args(ns)
    assert overrides == {
        "max_recording_duration": 45.0,
        "silence_duration": 2.5,
        "http_timeout": 90.0,
    }


def test_overrides_from_args_includes_only_set_values():
    from talkat.cli import _overrides_from_args

    ns = argparse.Namespace(max_recording=30.0, silence_duration=None, http_timeout=None)
    overrides = _overrides_from_args(ns)
    assert overrides == {"max_recording_duration": 30.0}


def test_overrides_from_args_handles_missing_attributes():
    """If an arg attribute is missing entirely (other subcommand) it's treated as None."""
    from talkat.cli import _overrides_from_args

    ns = argparse.Namespace()  # no max_recording / silence_duration / http_timeout at all
    assert _overrides_from_args(ns) == {}


# ---------------------------------------------------------------------------
# get_long_pid / get_listen_pid
# ---------------------------------------------------------------------------


def test_get_long_pid_returns_none_when_not_running(monkeypatch: pytest.MonkeyPatch):
    from talkat import cli as cli_mod
    from talkat import process_manager as pm_mod

    class FakePM:
        def __init__(self, name: str) -> None:
            self.name = name

        def is_running(self) -> tuple[bool, int | None]:
            return (False, None)

    monkeypatch.setattr(cli_mod, "ProcessManager", FakePM)
    monkeypatch.setattr(pm_mod, "ProcessManager", FakePM)

    assert cli_mod.get_long_pid() is None
    assert cli_mod.get_listen_pid() is None


def test_get_long_pid_returns_pid_when_running(monkeypatch: pytest.MonkeyPatch):
    from talkat import cli as cli_mod

    class FakePM:
        def __init__(self, name: str) -> None:
            self.name = name

        def is_running(self) -> tuple[bool, int]:
            return (True, 4242)

    monkeypatch.setattr(cli_mod, "ProcessManager", FakePM)

    assert cli_mod.get_long_pid() == 4242


# ---------------------------------------------------------------------------
# _start_long / _stop_long
# ---------------------------------------------------------------------------


def test_start_long_returns_zero_when_background_process_starts(
    monkeypatch: pytest.MonkeyPatch,
):
    from talkat import cli as cli_mod
    from talkat.process_manager import ProcessManager

    captured: dict[str, Any] = {}

    def fake_start(
        self: ProcessManager,
        cmd: list[str],
        debug: bool = False,
        env: dict | None = None,
    ) -> int:
        captured["cmd"] = cmd
        captured["env"] = env
        return 99

    monkeypatch.setattr(ProcessManager, "start_background_process", fake_start)

    pm = ProcessManager("long_dictation")
    rc = cli_mod._start_long(pm, debug=False)
    assert rc == 0
    # The spawned cmd must re-exec talkat in `long --background` mode.
    assert captured["cmd"][1:] == ["-m", "talkat.cli", "long", "--background"]


def test_start_long_passes_debug_env(monkeypatch: pytest.MonkeyPatch):
    from talkat import cli as cli_mod
    from talkat.process_manager import ProcessManager

    captured: dict[str, Any] = {}

    def fake_start(
        self: ProcessManager,
        cmd: list[str],
        debug: bool = False,
        env: dict | None = None,
    ) -> int:
        captured["debug"] = debug
        captured["env"] = env
        return 1

    monkeypatch.setattr(ProcessManager, "start_background_process", fake_start)

    pm = ProcessManager("long_dictation")
    cli_mod._start_long(pm, debug=True)
    assert captured["debug"] is True
    assert captured["env"] is not None
    assert captured["env"]["TALKAT_DEBUG"] == "1"


def test_start_long_returns_one_when_process_fails_to_start(
    monkeypatch: pytest.MonkeyPatch,
):
    from talkat import cli as cli_mod
    from talkat.process_manager import ProcessManager

    monkeypatch.setattr(ProcessManager, "start_background_process", lambda *_a, **_k: None)

    pm = ProcessManager("long_dictation")
    assert cli_mod._start_long(pm, debug=False) == 1


def test_stop_long_returns_zero_when_stop_succeeds(monkeypatch: pytest.MonkeyPatch):
    from talkat import cli as cli_mod
    from talkat.process_manager import ProcessManager

    monkeypatch.setattr(ProcessManager, "stop_process", lambda *_a, **_k: True)

    pm = ProcessManager("long_dictation")
    assert cli_mod._stop_long(pm) == 0


def test_stop_long_returns_one_when_stop_fails(monkeypatch: pytest.MonkeyPatch):
    from talkat import cli as cli_mod
    from talkat.process_manager import ProcessManager

    monkeypatch.setattr(ProcessManager, "stop_process", lambda *_a, **_k: False)

    pm = ProcessManager("long_dictation")
    assert cli_mod._stop_long(pm) == 1


# ---------------------------------------------------------------------------
# start_long_background / stop_long_background / toggle_long_background
# ---------------------------------------------------------------------------


def test_start_long_background_refuses_when_already_running(
    monkeypatch: pytest.MonkeyPatch,
):
    """If a long process is already running, start-long must return 1 without spawning."""
    from talkat import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_long_pid", lambda: 1234)

    spawn_calls: list = []
    monkeypatch.setattr(cli_mod, "_start_long", lambda *_a, **_k: spawn_calls.append("ran") or 0)

    rc = cli_mod.start_long_background(debug=False, try_only=True)
    assert rc == 1
    assert spawn_calls == [], "_start_long should not have been invoked"


def test_start_long_background_spawns_when_not_running(
    monkeypatch: pytest.MonkeyPatch, clean_pid_files
):
    from talkat import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_long_pid", lambda: None)
    monkeypatch.setattr(cli_mod, "_start_long", lambda *_a, **_k: 0)

    assert cli_mod.start_long_background(debug=False, try_only=True) == 0


def test_start_long_background_returns_one_on_lock_timeout(
    monkeypatch: pytest.MonkeyPatch, clean_pid_files
):
    """If the lock can't be acquired, return 1 (not raise)."""
    from talkat import cli as cli_mod
    from talkat.process_manager import ProcessManager

    # Hold the lock from another instance.
    holder = ProcessManager("long_dictation")
    assert holder.acquire_lock(timeout=0.5)
    try:
        assert cli_mod.start_long_background(debug=False, try_only=True) == 1
    finally:
        holder.release_lock()


def test_stop_long_background_dispatches_to_stop_long(monkeypatch: pytest.MonkeyPatch):
    from talkat import cli as cli_mod

    calls: list = []
    monkeypatch.setattr(cli_mod, "_stop_long", lambda _pm: calls.append("stop") or 0)

    assert cli_mod.stop_long_background(try_only=True) == 0
    assert calls == ["stop"]


def test_toggle_long_background_dispatches_to_pm_toggle(monkeypatch: pytest.MonkeyPatch):
    from talkat import cli as cli_mod
    from talkat.process_manager import ProcessManager

    calls: list[dict] = []

    def fake_toggle(self: ProcessManager, start, try_only: bool = False) -> int:
        calls.append({"try_only": try_only})
        # Invoke the start callback so we know it was wired correctly.
        return start()

    monkeypatch.setattr(ProcessManager, "toggle", fake_toggle)
    monkeypatch.setattr(cli_mod, "_start_long", lambda _pm, _debug: 42)

    rc = cli_mod.toggle_long_background(debug=False, try_only=True)
    assert rc == 42
    assert calls == [{"try_only": True}]


def test_stop_listen_process_returns_one_when_no_active_listen(
    monkeypatch: pytest.MonkeyPatch,
):
    """When stop_process returns False (no live PID), stop-listen must return 1."""
    from talkat import cli as cli_mod
    from talkat.process_manager import ProcessManager

    monkeypatch.setattr(ProcessManager, "stop_process", lambda *_a, **_k: False)

    rc = cli_mod.stop_listen_process(try_only=True)
    assert rc == 1


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
    from talkat import cli as cli_mod

    captured: dict = {}

    def fake_handler(
        file_path: str,
        output_file: str | None = None,
        output_format: str = "text",
        clipboard: bool = False,
    ) -> int:
        captured["file"] = file_path
        captured["output"] = output_file
        captured["format"] = output_format
        captured["clipboard"] = clipboard
        return 0

    monkeypatch.setattr(cli_mod, "process_audio_file_command", fake_handler)

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
    }


def test_main_batch_dispatches_to_batch_process_files(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from talkat import cli as cli_mod

    captured: dict = {}

    def fake_handler(
        files: list[str], output_dir: str | None = None, output_format: str = "text"
    ) -> int:
        captured["files"] = files
        captured["dir"] = output_dir
        captured["format"] = output_format
        return 0

    monkeypatch.setattr(cli_mod, "batch_process_files", fake_handler)

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


def test_main_start_long_dispatches_to_start_long_background(
    monkeypatch: pytest.MonkeyPatch,
):
    from talkat import cli as cli_mod

    captured: dict = {}

    def fake(debug: bool = False, try_only: bool = False) -> int:
        captured["debug"] = debug
        captured["try_only"] = try_only
        return 0

    monkeypatch.setattr(cli_mod, "start_long_background", fake)
    monkeypatch.setattr(sys, "argv", ["talkat", "start-long", "--try-lock"])

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured == {"debug": False, "try_only": True}


def test_main_stop_long_dispatches_to_stop_long_background(
    monkeypatch: pytest.MonkeyPatch,
):
    from talkat import cli as cli_mod

    captured: dict = {}

    def fake(try_only: bool = False) -> int:
        captured["try_only"] = try_only
        return 0

    monkeypatch.setattr(cli_mod, "stop_long_background", fake)
    monkeypatch.setattr(sys, "argv", ["talkat", "stop-long"])

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured == {"try_only": False}


def test_main_toggle_long_dispatches_to_toggle_long_background(
    monkeypatch: pytest.MonkeyPatch,
):
    from talkat import cli as cli_mod

    captured: dict = {}

    def fake(debug: bool = False, try_only: bool = False) -> int:
        captured["debug"] = debug
        captured["try_only"] = try_only
        return 0

    monkeypatch.setattr(cli_mod, "toggle_long_background", fake)
    monkeypatch.setattr(sys, "argv", ["talkat", "--debug", "toggle-long", "--try-lock"])

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured == {"debug": True, "try_only": True}


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


def test_main_listen_dispatches_to_listen_once_with_overrides(
    monkeypatch: pytest.MonkeyPatch, clean_pid_files
):
    """listen subcommand routes to listen_once and passes timeout overrides through."""
    from talkat import main as main_mod
    from talkat.process_manager import ProcessManager

    captured: dict = {}

    def fake_listen_once(
        output_file: str | None = None, config_overrides: dict | None = None
    ) -> int:
        captured["output_file"] = output_file
        captured["overrides"] = config_overrides
        return 0

    monkeypatch.setattr(main_mod, "listen_once", fake_listen_once)
    # Defang the lock + PID write so we don't need real microphone setup.
    monkeypatch.setattr(ProcessManager, "is_running", lambda _self: (False, None))
    monkeypatch.setattr(ProcessManager, "write_pid", lambda _self, _pid: None)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "talkat",
            "listen",
            "-o",
            "/tmp/out.txt",
            "--max-recording",
            "20",
            "--silence-duration",
            "1.5",
            "--http-timeout",
            "45",
        ],
    )

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured["output_file"] == "/tmp/out.txt"
    assert captured["overrides"] == {
        "max_recording_duration": 20.0,
        "silence_duration": 1.5,
        "http_timeout": 45.0,
    }


def test_main_listen_with_try_lock_refuses_when_already_running(
    monkeypatch: pytest.MonkeyPatch, clean_pid_files
):
    """`listen --try-lock` must exit 1 if another listen process is already active."""
    from talkat.process_manager import ProcessManager

    monkeypatch.setattr(ProcessManager, "is_running", lambda _self: (True, 9999))
    monkeypatch.setattr(sys, "argv", ["talkat", "listen", "--try-lock"])

    assert _run_main(sys.argv) == 1


def test_main_long_dispatches_to_listen_continuous(
    monkeypatch: pytest.MonkeyPatch, clean_pid_files
):
    from talkat import main as main_mod

    captured: dict = {}

    def fake_listen_continuous(
        output_file: str | None = None,
        background: bool = False,
        clipboard: bool = True,
        config_overrides: dict | None = None,
    ) -> int:
        captured["output_file"] = output_file
        captured["background"] = background
        captured["clipboard"] = clipboard
        captured["overrides"] = config_overrides
        return 0

    monkeypatch.setattr(main_mod, "listen_continuous", fake_listen_continuous)
    monkeypatch.setattr(
        sys, "argv", ["talkat", "long", "--no-clipboard", "--silence-duration", "5"]
    )

    rc = _run_main(sys.argv)
    assert rc == 0
    assert captured["background"] is False
    assert captured["clipboard"] is False
    assert captured["overrides"] == {"silence_duration": 5.0}
