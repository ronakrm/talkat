"""Tests for talkat.doctor — helper logic and exit-code semantics.

run_doctor's individual checks talk to systemd, PortAudio, and the server
socket; those are stubbed here. What we pin down: PATH-shadowing detection,
the report's fail semantics (✗ → exit 1, warnings don't fail), and that a
healthy stubbed environment exits 0.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from talkat import doctor as doctor_mod

# ---------------------------------------------------------------------------
# _talkat_binaries_on_path
# ---------------------------------------------------------------------------


def _make_fake_talkat(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / "talkat"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_binaries_on_path_orders_and_dedupes(tmp_path, monkeypatch: pytest.MonkeyPatch):
    first = _make_fake_talkat(tmp_path / "a")
    second = _make_fake_talkat(tmp_path / "b")
    path = os.pathsep.join([str(first.parent), str(second.parent), str(first.parent)])
    monkeypatch.setenv("PATH", path)

    found = doctor_mod._talkat_binaries_on_path()
    assert found == [str(first), str(second)]


def test_binaries_on_path_empty_when_none(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert doctor_mod._talkat_binaries_on_path() == []


# ---------------------------------------------------------------------------
# _Report / run_doctor exit semantics
# ---------------------------------------------------------------------------


def test_report_fails_only_on_bad(capsys):
    report = doctor_mod._Report()
    report.line(doctor_mod.OK, "fine")
    report.line(doctor_mod.WARN, "meh")
    assert not report.failed
    report.line(doctor_mod.BAD, "broken")
    assert report.failed


def _stub_all_checks(monkeypatch: pytest.MonkeyPatch, *, fail_one: bool = False) -> None:
    def ok_check(report: doctor_mod._Report) -> None:
        report.line(doctor_mod.OK, "stubbed")

    def bad_check(report: doctor_mod._Report) -> None:
        report.line(doctor_mod.BAD, "stubbed failure")

    monkeypatch.setattr(doctor_mod, "_check_install", ok_check)
    monkeypatch.setattr(doctor_mod, "_check_service", bad_check if fail_one else ok_check)
    monkeypatch.setattr(doctor_mod, "_check_desktop_tools", ok_check)
    monkeypatch.setattr(doctor_mod, "_check_audio", ok_check)
    monkeypatch.setattr(doctor_mod, "_check_config", ok_check)


def test_run_doctor_healthy_returns_zero(monkeypatch: pytest.MonkeyPatch, capsys):
    _stub_all_checks(monkeypatch)
    assert doctor_mod.run_doctor() == 0
    assert "healthy" in capsys.readouterr().out


def test_run_doctor_failure_returns_one(monkeypatch: pytest.MonkeyPatch, capsys):
    _stub_all_checks(monkeypatch, fail_one=True)
    assert doctor_mod.run_doctor() == 1
    assert "problems found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _check_service warning on unit shadowing
# ---------------------------------------------------------------------------


def test_check_service_warns_when_user_unit_shadows_packaged(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    user_unit = str(tmp_path / "user" / "talkat.service")
    packaged = tmp_path / "packaged" / "talkat.service"
    packaged.parent.mkdir(parents=True)
    packaged.write_text("[Unit]\n")

    monkeypatch.setattr(doctor_mod, "PACKAGED_UNIT", packaged)
    monkeypatch.setattr(doctor_mod, "_systemd_unit_info", lambda: (user_unit, "active"))
    monkeypatch.setattr(doctor_mod, "_socket_exists", lambda _s: True)
    monkeypatch.setattr(doctor_mod, "_server_health", lambda _s: {"model_type": "faster-whisper"})

    report = doctor_mod._Report()
    doctor_mod._check_service(report)
    out = capsys.readouterr().out
    assert "unit shadowing" in out
    assert not report.failed  # shadowing is a warning, not a failure


def test_check_service_flags_dead_server(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(doctor_mod, "_systemd_unit_info", lambda: (None, None))
    monkeypatch.setattr(doctor_mod, "_server_health", lambda _s: None)

    report = doctor_mod._Report()
    doctor_mod._check_service(report)
    assert report.failed
    assert "not responding" in capsys.readouterr().out
