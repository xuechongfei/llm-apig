from desktop.tray import make_icon_image


def test_make_icon_image():
    img = make_icon_image()
    assert img.size == (64, 64)
    assert img.mode == "RGBA"
