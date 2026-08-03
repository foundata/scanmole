"""Tests for external tool lookup and the distro-aware install hint."""

from __future__ import annotations

import pytest

from scanmole.errors import MissingDependencyError
from scanmole.external import hint_for_distro, parse_distro_ids, require_tools

_FEDORA_OS_RELEASE = 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=44\n'
_UBUNTU_OS_RELEASE = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n'
_DEBIAN_OS_RELEASE = 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n'
_MINT_OS_RELEASE = 'ID=linuxmint\nID_LIKE="ubuntu debian"\n'


def test_parse_distro_ids_reads_id_and_id_like() -> None:
    assert parse_distro_ids(_UBUNTU_OS_RELEASE) == {"ubuntu", "debian"}
    assert parse_distro_ids(_MINT_OS_RELEASE) == {"linuxmint", "ubuntu", "debian"}
    assert parse_distro_ids("") == set()


def test_hint_names_apt_packages_on_debian_family() -> None:
    for text in (_UBUNTU_OS_RELEASE, _DEBIAN_OS_RELEASE, _MINT_OS_RELEASE):
        hint = hint_for_distro(parse_distro_ids(text))
        assert "apt install" in hint
        assert "sane-utils" in hint
        assert "tesseract-ocr-deu" in hint


def test_hint_defaults_to_fedora_packages() -> None:
    for text in (_FEDORA_OS_RELEASE, ""):
        hint = hint_for_distro(parse_distro_ids(text))
        assert "dnf install" in hint
        assert "sane-backends" in hint
        assert "tesseract-langpack-deu" in hint


def test_require_tools_accepts_present_tools() -> None:
    require_tools(["sh"])


def test_require_tools_reports_missing_tools_with_the_hint() -> None:
    with pytest.raises(MissingDependencyError, match="no-such-tool-xyz") as info:
        require_tools(["sh", "no-such-tool-xyz"])

    assert "install" in info.value.message
