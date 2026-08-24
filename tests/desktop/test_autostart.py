import sys
import winreg

from desktop import autostart

TEST_KEY = r"Software\llm-apig-test"


def _cleanup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software") as parent:
            winreg.DeleteKey(parent, "llm-apig-test")
    except OSError:
        pass


def test_roundtrip():
    _cleanup()
    try:
        assert not autostart.is_enabled(TEST_KEY)
        autostart.enable(TEST_KEY)
        assert autostart.is_enabled(TEST_KEY)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as k:
            value, _ = winreg.QueryValueEx(k, "llm-apig")
        assert value == sys.executable
        autostart.disable(TEST_KEY)
        assert not autostart.is_enabled(TEST_KEY)
    finally:
        _cleanup()


def test_disable_when_absent_is_noop():
    _cleanup()
    autostart.disable(TEST_KEY)  # 不抛错
    assert not autostart.is_enabled(TEST_KEY)
