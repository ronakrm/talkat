"""Tests for talkat.focus — compositor detection and focused-window queries.

No real compositor IPC is exercised: ``_run_json`` (or the subprocess layer
under it) is stubbed. What we assert is the contract the focus guard relies
on: correct compositor selection from env vars, correct id extraction per
compositor, and ``None`` on every failure path (never a false "changed").
"""

from __future__ import annotations

import pytest

from talkat import focus as focus_mod


@pytest.fixture
def no_compositor_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip all supported compositor env markers; tests opt back in per-case."""
    for var in ("NIRI_SOCKET", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# compositor_name
# ---------------------------------------------------------------------------


def test_compositor_name_none_without_markers(no_compositor_env):
    assert focus_mod.compositor_name() is None


@pytest.mark.parametrize(
    ("var", "expected"),
    [
        ("NIRI_SOCKET", "niri"),
        ("HYPRLAND_INSTANCE_SIGNATURE", "hyprland"),
        ("SWAYSOCK", "sway"),
    ],
)
def test_compositor_name_detects_from_env(no_compositor_env, var: str, expected: str):
    no_compositor_env.setenv(var, "/tmp/fake.sock")
    assert focus_mod.compositor_name() == expected


# ---------------------------------------------------------------------------
# get_focused_window
# ---------------------------------------------------------------------------


def test_focused_window_none_without_compositor(no_compositor_env):
    assert focus_mod.get_focused_window() is None


def test_focused_window_niri(no_compositor_env):
    no_compositor_env.setenv("NIRI_SOCKET", "/tmp/fake.sock")
    no_compositor_env.setattr(focus_mod, "_run_json", lambda cmd: {"id": 42, "title": "editor"})
    assert focus_mod.get_focused_window() == "niri:42"


def test_focused_window_niri_no_window(no_compositor_env):
    """niri returns null when nothing is focused → None (guard skipped)."""
    no_compositor_env.setenv("NIRI_SOCKET", "/tmp/fake.sock")
    no_compositor_env.setattr(focus_mod, "_run_json", lambda cmd: None)
    assert focus_mod.get_focused_window() is None


def test_focused_window_hyprland(no_compositor_env):
    no_compositor_env.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sig")
    no_compositor_env.setattr(focus_mod, "_run_json", lambda cmd: {"address": "0xabc123"})
    assert focus_mod.get_focused_window() == "hyprland:0xabc123"


def test_focused_window_sway_walks_tree(no_compositor_env):
    no_compositor_env.setenv("SWAYSOCK", "/tmp/fake.sock")
    tree = {
        "id": 1,
        "focused": False,
        "nodes": [
            {"id": 2, "focused": False, "nodes": [], "floating_nodes": []},
            {
                "id": 3,
                "focused": False,
                "nodes": [{"id": 7, "focused": True, "nodes": [], "floating_nodes": []}],
                "floating_nodes": [],
            },
        ],
        "floating_nodes": [],
    }
    no_compositor_env.setattr(focus_mod, "_run_json", lambda cmd: tree)
    assert focus_mod.get_focused_window() == "sway:7"


def test_focused_window_sway_no_focused_node(no_compositor_env):
    no_compositor_env.setenv("SWAYSOCK", "/tmp/fake.sock")
    tree = {"id": 1, "focused": False, "nodes": [], "floating_nodes": []}
    no_compositor_env.setattr(focus_mod, "_run_json", lambda cmd: tree)
    assert focus_mod.get_focused_window() is None


# ---------------------------------------------------------------------------
# _run_json failure paths
# ---------------------------------------------------------------------------


def test_run_json_returns_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch):
    class _Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(focus_mod, "safe_subprocess_run", lambda *a, **k: _Failed())
    assert focus_mod._run_json(["niri", "msg", "--json", "focused-window"]) is None


def test_run_json_returns_none_on_exception(monkeypatch: pytest.MonkeyPatch):
    def boom(*_a: object, **_k: object) -> object:
        raise FileNotFoundError("niri not installed")

    monkeypatch.setattr(focus_mod, "safe_subprocess_run", boom)
    assert focus_mod._run_json(["niri", "msg", "--json", "focused-window"]) is None


def test_run_json_parses_stdout(monkeypatch: pytest.MonkeyPatch):
    class _Ok:
        returncode = 0
        stdout = '{"id": 5}'

    monkeypatch.setattr(focus_mod, "safe_subprocess_run", lambda *a, **k: _Ok())
    assert focus_mod._run_json(["niri", "msg", "--json", "focused-window"]) == {"id": 5}
