import ctypes

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "llm-apig-singleton"
WINDOW_TITLE = "llm-apig"


class SingleInstance:
    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = None

    def acquire(self) -> bool:
        handle = self._k32.CreateMutexW(None, False, self.name)
        if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # 未获得所有权：立即关闭刚打开的句柄，避免泄漏的句柄
            # 在 release() 之后仍使命名互斥锁存续。
            if handle:
                self._k32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


def activate_existing_window(title: str = WINDOW_TITLE) -> bool:
    u32 = ctypes.WinDLL("user32")
    hwnd = u32.FindWindowW(None, title)
    if not hwnd:
        return False
    u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    u32.SetForegroundWindow(hwnd)
    return True
