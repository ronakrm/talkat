"""Tests for talkat.service — unit file generation and path layout."""

from talkat.service import SERVICE_DIR, SERVICE_FILE, SERVICE_NAME, _service_unit


def test_service_name_is_talkat_service():
    assert SERVICE_NAME == "talkat.service"


def test_service_file_under_xdg_config_systemd_user():
    # conftest sets XDG_CONFIG_HOME to a tmpdir; the service file should live
    # under <XDG_CONFIG_HOME>/systemd/user/talkat.service.
    assert SERVICE_FILE.parent == SERVICE_DIR
    assert SERVICE_FILE.name == "talkat.service"
    assert SERVICE_DIR.parts[-2:] == ("systemd", "user")


def test_service_unit_uses_given_python():
    unit = _service_unit("/opt/custom/python")
    assert "ExecStart=/opt/custom/python -m talkat.cli server" in unit


def test_service_unit_has_required_sections():
    unit = _service_unit("/usr/bin/python3")
    for header in ("[Unit]", "[Service]", "[Install]"):
        assert header in unit


def test_service_unit_wantedby_default_target():
    # Default target is correct for --user services (multi-user.target is for system).
    unit = _service_unit("/usr/bin/python3")
    assert "WantedBy=default.target" in unit


def test_service_unit_no_user_directive():
    # --user services run as the invoking user by default; an explicit User=
    # directive on a user-scope unit is wrong and would fail to load.
    unit = _service_unit("/usr/bin/python3")
    assert "\nUser=" not in unit
