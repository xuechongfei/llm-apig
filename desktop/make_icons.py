"""从托盘同款图形生成 src-tauri/icons/（改图标时手动跑一次）。

产物：icon.ico(16/32/48/256) + 32x32.png + 128x128.png + 128x128@2x.png
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src-tauri" / "icons"


def make_icon_image() -> Image.Image:
    """与网页 favicon 同款：深蓝圆角块 + 三条横线"""
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([16, 16, 496, 496], radius=112, fill=(15, 43, 70, 255))
    d.rectangle([112, 176, 400, 216], fill=(255, 255, 255, 235))
    d.rectangle([112, 248, 304, 288], fill=(127, 176, 221, 235))
    d.rectangle([112, 320, 352, 360], fill=(255, 255, 255, 235))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img = make_icon_image()
    img.save(OUT / "icon.ico",
             sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    img.resize((32, 32), Image.LANCZOS).save(OUT / "32x32.png")
    img.resize((128, 128), Image.LANCZOS).save(OUT / "128x128.png")
    img.resize((256, 256), Image.LANCZOS).save(OUT / "128x128@2x.png")
    print(f"图标已生成: {OUT}")


if __name__ == "__main__":
    main()
