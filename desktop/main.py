import ctypes
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 开发模式兜底

from desktop import paths, server as dserver, singleinstance

log = logging.getLogger("llm-apig")


def show_error(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(0, text, "llm-apig", 0x10)


def main() -> None:
    lock = singleinstance.SingleInstance()
    if not lock.acquire():
        singleinstance.activate_existing_window()
        return
    try:
        data = paths.data_dir()
        data.mkdir(parents=True, exist_ok=True)
        os.environ["LLMAPIG_DATA_DIR"] = str(data)
        paths.setup_logging()
        from app.db import init_db
        from app.main import app
        from app.update_check import current_version
        init_db()
        gw = dserver.GatewayServer(app)
        port = gw.start()
        (data / "runtime.json").write_text(
            json.dumps({"port": port, "version": current_version()}),
            encoding="utf-8")
        log.info("网关就绪 端口=%s 版本=%s", port, current_version())
    except Exception as e:
        log.exception("启动失败")
        show_error(f"启动失败：{e}\n\n日志：{paths.log_dir() / 'app.log'}")
        lock.release()
        return

    import httpx
    import webview

    window = webview.create_window(
        "llm-apig", f"http://127.0.0.1:{port}/admin", width=1200, height=800)

    icon = None
    quitting = False

    def on_closing():
        if quitting or icon is None:  # 正在退出 / 无托盘时直接放行关闭
            return
        window.hide()  # 缩到托盘，服务继续
        return False

    window.events.closing += on_closing

    def on_open():
        window.show()

    def on_check_update():
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/admin/api/update?fresh=1",
                          timeout=15)
            info = r.json().get("update")
        except httpx.HTTPError:
            info = None
            ctypes.windll.user32.MessageBoxW(
                0, "检查更新失败，请稍后再试", "llm-apig", 0x30)
            return
        if info:
            ctypes.windll.user32.MessageBoxW(
                0, f"新版本 {info['latest']} 可用。\n{info['notes']}\n\n"
                   f"请到发布页下载：{info['url']}", "llm-apig 更新", 0x40)
        else:
            ctypes.windll.user32.MessageBoxW(
                0, "已是最新版本", "llm-apig", 0x40)

    def on_quit():
        nonlocal quitting
        quitting = True
        window.destroy()

    try:
        from desktop import tray
        icon = tray.create_tray(on_open, on_check_update, on_quit)
        icon.run_detached()
    except Exception:
        log.exception("托盘初始化失败（不影响使用）")

    try:
        webview.start()  # 阻塞直到窗口销毁（退出）
    except Exception as e:
        log.exception("窗口启动失败")
        show_error(f"界面启动失败：{e}\n\n若提示 WebView2 相关错误，请安装 "
                   f"WebView2 运行时：https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
                   f"日志：{paths.log_dir() / 'app.log'}")
        if icon:
            icon.stop()
        gw.stop()
        lock.release()
        return

    if icon:
        icon.stop()
    gw.stop()
    lock.release()
    log.info("已退出")


if __name__ == "__main__":
    main()
