import os

from desktop import singleinstance


def _uniq():
    return f"llm-apig-test-{os.getpid()}-{id(object())}"


def test_second_acquire_fails():
    name = _uniq()
    a = singleinstance.SingleInstance(name)
    b = singleinstance.SingleInstance(name)
    assert a.acquire()
    assert not b.acquire()
    a.release()
    c = singleinstance.SingleInstance(name)
    assert c.acquire()
    c.release()


def test_activate_missing_window_returns_false():
    assert singleinstance.activate_existing_window("绝不存在的窗口标题-xyz") is False
