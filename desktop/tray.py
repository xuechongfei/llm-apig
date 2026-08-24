import ctypes

import pystray
from PIL import Image, ImageDraw

from desktop import autostart


def make_icon_image() -> Image.Image:
    """与网页 favicon 同款：深蓝圆角块 + 三条横线"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(15, 43, 70, 255))
    d.rectangle([14, 22, 50, 27], fill=(255, 255, 255, 235))
    d.rectangle([14, 31, 38, 36], fill=(127, 176, 221, 235))
    d.rectangle([14, 40, 44, 45], fill=(255, 255, 255, 235))
    return img


def create_tray(on_open, on_check_update, on_quit) -> pystray.Icon:
    def _autostart_toggle(icon, item):
        if not autostart.is_supported():
            ctypes.windll.user32.MessageBoxW(
                0, "开发模式下不可设置开机自启", "llm-apig", 0x40)
            return
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable()

    menu = pystray.Menu(
        pystray.MenuItem("打开主界面", lambda icon, item: on_open(),
                         default=True),
        pystray.MenuItem("开机自启", _autostart_toggle,
                         checked=lambda item: autostart.is_enabled()),
        pystray.MenuItem("检查更新", lambda icon, item: on_check_update()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda icon, item: on_quit()),
    )
    return pystray.Icon("llm-apig", make_icon_image(), "llm-apig API 网关",
                        menu)
