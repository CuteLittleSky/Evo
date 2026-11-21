"""Evo 窗口的启动画面（splash.py、app_window.py 的 page_shown / remember_theme、base.html 里调它们的那几句）。

- 颜色、标题栏的尺寸和面板的样式表（style.css）一样：网页换上来时标题栏不跳
- 主题：面板选的深色 / 浅色照用；跟随系统、没记过时看 Windows 的深浅色
- 位置：图标、字、加载条在标题栏下面居中，按 Windows 的缩放比例放大；加载条走动的那一段不出条
- 那行字：assets/splash/ 里的小字体文件真的有 TAGLINE 里的每个字（缺字会画成方框）
- 真的用 WinForms 画一张（不显示窗口）：标题栏、图标、加载条都画上了；拿掉以后控件也没了
- 窗口记主题：只记认得的值，没变就不写 settings.json；网页画好、换主题时会调

运行：python tests/test_splash.py
"""

import re
import threading
from pathlib import Path
from unittest import mock

from fake_game import ROOT, report  # 要在项目代码之前导入（设置临时数据文件夹）

from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: E402

import app_window  # noqa: E402
import splash  # noqa: E402

CSS = (ROOT / "panel" / "static" / "style.css").read_text(encoding="utf-8")
BASE = (ROOT / "panel" / "templates" / "base.html").read_text(encoding="utf-8")
CSS_NAMES = {"bg": "--bg", "titlebar": "--titlebar-bg", "border": "--border", "muted": "--muted", "track": "--nav-on-bg"}


def css_block(selector):
    start = CSS.index(selector + " {")
    return CSS[start:CSS.index("\n}", start)]


def css_var(block, name):
    return re.search(rf"{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})", block).group(1).upper()


def style_checks():
    blocks = {"dark": css_block(':root, [data-theme="dark"]'), "light": css_block('[data-theme="light"]')}
    same = all(splash.THEMES[theme][key].upper() == css_var(blocks[theme], name)
               for theme in blocks for key, name in CSS_NAMES.items())
    titlebar = re.search(r"\.titlebar \{[^}]*height: (\d+)px", CSS).group(1)
    button = re.search(r"\.win-btn \{[^}]*width: (\d+)px", CSS).group(1)
    pad = re.search(r"\.titlebar-drag \{[^}]*gap: (\d+)px; padding-left: (\d+)px", CSS).groups()
    name = re.search(r"\.titlebar-name \{[^}]*font-size: (\d+)px; letter-spacing: (\d+)px", CSS).groups()
    return {
        "两个主题的颜色和 style.css 一样（底色、标题栏、边框、灰字、加载条的底）": same,
        "标题栏高度、按钮宽度、图标和字的间距、EVO 的字号和字间距和 style.css 一样":
        (int(titlebar), int(button), int(pad[0]), int(pad[1]), int(name[0]), int(name[1]))
        == (splash.TITLEBAR_H, splash.WIN_BTN_W, splash.TITLE_GAP, splash.TITLE_PAD,
            splash.TITLE_FONT[1], splash.TITLE_FONT[2]),
    }


def theme_checks():
    with mock.patch.object(splash.winreg, "OpenKey", side_effect=OSError("没有这一项")):
        unreadable = splash.windows_light()
    return {
        "面板选了深色 / 浅色：照用，不管 Windows": splash.resolve_theme("dark", light=True) == "dark"
        and splash.resolve_theme("light", light=False) == "light",
        "跟随系统、没记过、记的值认不得：看 Windows 的深浅色":
        [splash.resolve_theme(c, light=True) for c in ("system", None, "blue")] == ["light"] * 3
        and [splash.resolve_theme(c, light=False) for c in ("system", None, "blue")] == ["dark"] * 3,
        "读不到 Windows 的设置：当深色": unreadable is False,
    }


def layout_checks():
    sizes = {"icon": 48, "tagline": (90, 14)}
    at = splash.layout(1280, 860, 1.0, sizes)
    ix, iy = at["icon"]
    tx, ty = at["tagline"]
    bx, by, bw, bh = at["bar"]
    group_top, group_bottom = iy, by + bh
    hi = splash.layout(1600, 1075, 1.25, {"icon": 60, "tagline": (112, 18)})
    segs = [splash.bar_segment(t / 100, 56) for t in range(0, 300)]
    shown = [s for s in segs if s]
    return {
        "图标、字、加载条左右居中": ix + 24 == 640 and tx + 45 == 640 and bx + bw // 2 == 640,
        "从上到下：图标、字、加载条，中间隔开": iy + 48 < ty and ty + 14 < by,
        "一组东西在标题栏下面上下居中":
        abs((group_top - at["titlebar"]) - (860 - group_bottom)) <= 1 and group_top > at["titlebar"],
        "标题栏 34 像素，三个按钮各 46 像素靠右": at["titlebar"] == 34 and at["buttons"] == [1280 - 138, 1280 - 92, 1280 - 46],
        "缩放 125%：标题栏、按钮、加载条跟着放大，边框还是 1 像素":
        hi["titlebar"] == round(34 * 1.25) and hi["buttons"][2] == 1600 - round(46 * 1.25)
        and hi["bar"][2] == round(splash.BAR_W * 1.25) and hi["line"] == 1,
        "缩放 200%：边框 2 像素": splash.layout(2560, 1720, 2.0, sizes)["line"] == 2,
        "加载条：走动的那一段从不出条，一趟里有在动": all(0 <= a < b <= 56 for a, b in shown)
        and len({a for a, _ in shown}) > 10,
        "加载条：刚开始那一刻还在条外面（从左边进来）": segs[0] is None,
    }


def notdef_mask(font):
    return font.getmask("￿")  # 字体里一定没有的字：画出来的就是"缺字"的样子


def has_glyph(font, ch):
    mask, missing = font.getmask(ch), notdef_mask(font)
    if mask.getbbox() is None:
        return False
    if mask.size != missing.size:
        return True
    a, b = Image.new("L", mask.size), Image.new("L", missing.size)
    a.putdata(list(mask))
    b.putdata(list(missing))
    return ImageChops.difference(a, b).getbbox() is not None


def font_checks():
    present = all(has_glyph(ImageFont.truetype(str(splash.FONT_DIR / f), 40), ch)
                  for text, f, _, _ in splash.TAGLINE for ch in text)
    detects_missing = not has_glyph(ImageFont.truetype(str(splash.FONT_DIR / splash.TAGLINE[0][1]), 40), "Q")
    small = sum((splash.FONT_DIR / f).stat().st_size for _, f, _, _ in splash.TAGLINE)
    img = splash.text_image([(t, splash.FONT_DIR / f, z, s) for t, f, z, s in splash.TAGLINE], 1.0, "#9AA3B2",
                            splash.TAGLINE_GAP)
    img2 = splash.text_image([(t, splash.FONT_DIR / f, z, s) for t, f, z, s in splash.TAGLINE], 2.0, "#9AA3B2",
                             splash.TAGLINE_GAP)
    font, size, spacing = splash.TITLE_FONT
    title = splash.text_image([("EVO", font, size, spacing)], 1.0, "#9AA3B2")
    return {
        "那行字的每个字字体里都有（不会画成方框）": present,
        "（检查本身有效：字体里没有的字认得出来）": detects_missing,
        "字体文件很小（只含用到的几个字）": small < 20_000,
        "许可证在旁边": (splash.FONT_DIR / "OFL.txt").is_file(),
        "那行字画出来了：透明底、有字，200% 时大约两倍大": img.getchannel("A").getbbox() is not None
        and img.getpixel((0, 0))[3] == 0 and 1.8 < img2.width / img.width < 2.2,
        "标题栏的 EVO 用面板的 Chakra Petch 画得出来": title.getchannel("A").getbbox() is not None,
    }


def draw_checks():
    """真的建一个 WinForms 窗口（不显示），盖上启动画面，画进一张图里看。WinForms 要在 STA 线程里。"""
    result = {}

    def run():
        from System.Drawing import Bitmap, Rectangle, Size
        from System.Windows.Forms import Form

        form = Form()
        form.ClientSize = Size(1280, 860)
        form.CreateControl()
        sp = splash.Splash(form, "dark")
        sp.scale = 1.0  # 不管这台电脑的缩放，按 100% 画
        result["盖上了"] = form.Controls.Count == 1 and form.Controls[0].Equals(sp.box)  # pythonnet 每次包一层新的，不能用 is
        bmp = Bitmap(1280, 860)
        sp.box.Size = Size(1280, 860)
        sp.box.DrawToBitmap(bmp, Rectangle(0, 0, 1280, 860))

        def px(x, y):
            c = bmp.GetPixel(x, y)
            return "#%02X%02X%02X" % (c.R, c.G, c.B)

        at = splash.layout(1280, 860, 1.0, {"icon": sp.images["icon"].Width,
                                            "tagline": (sp.images["tagline"].Width, sp.images["tagline"].Height)})
        bx, by, bw, _ = at["bar"]
        result["px"] = {
            "titlebar": px(600, 10), "border": px(600, at["titlebar"] - 1), "bg": px(100, 400),
            "icon": px(at["icon"][0] + 24, at["icon"][1] + 24), "track": px(bx + bw - 1, by),
            "close": [px(1280 - 23 + d, 17 + d) for d in range(-3, 4)],
        }
        sp._remove_now()
        result["拿掉了"] = form.Controls.Count == 0
        form.Dispose()

    from webview.platforms import winforms  # noqa: F401  加载 pythonnet 和 WinForms（和窗口进程里一样）

    import System.Threading as T

    error = []

    def guarded():
        try:
            run()
        except Exception as e:  # noqa: BLE001
            error.append(repr(e))

    thread = T.Thread(T.ThreadStart(guarded))
    thread.SetApartmentState(T.ApartmentState.STA)
    thread.Start()
    thread.Join()
    if error:
        print("  画的时候出错：", error[0])
        return {"真的用 WinForms 画得出来": False}
    p, c = result["px"], splash.THEMES["dark"]
    return {
        "盖在窗口上（拿掉以前是窗口里唯一的控件）": result["盖上了"],
        "标题栏、下边框、底色的颜色对": (p["titlebar"], p["border"], p["bg"]) == (c["titlebar"], c["border"], c["bg"]),
        "中间画了图标（不是底色）": p["icon"] != c["bg"],
        "画了加载条的底": p["track"] == c["track"],
        "右上角画了关闭的叉": any(v != c["titlebar"] for v in p["close"]),
        "拿掉以后控件也没了": result["拿掉了"],
    }


class FakeSplash:
    def __init__(self):
        self.removed = 0

    def remove(self):
        self.removed += 1


def api_checks():
    api = app_window.WindowApi()
    writes = []
    stored = {"window_theme": "dark"}
    with mock.patch.object(app_window.app_settings, "get", lambda k, d=None: stored.get(k, d)), \
         mock.patch.object(app_window.app_settings, "set_value",
                           lambda k, v: (writes.append((k, v)), stored.__setitem__(k, v))):
        api.remember_theme("dark")  # 没变
        api.remember_theme("blue")  # 认不得
        api.remember_theme(None)
        api.remember_theme("light")
        api.page_shown("light")  # 没有启动画面也不出错
        fake = FakeSplash()
        api._splash = fake
        api.page_shown("system")
        api.page_shown("system")
    return {
        "主题没变、认不得的值：不写 settings.json；变了才写": writes == [("window_theme", "light"), ("window_theme", "system")],
        "网页画好了：拿掉启动画面（调几次都行）": fake.removed == 2,
        "base.html：网页画好以后调 page_shown，带上现在选的主题": "api().page_shown(evoTheme())" in BASE
        and "requestAnimationFrame" in BASE,
        "base.html：换主题时告诉窗口": "winApi.remember_theme(choice)" in BASE,
        "app_window.run 用记下的主题定底色、在窗口显示之前盖上启动画面":
        "splash.resolve_theme(app_settings.get(\"window_theme\"))" in Path(app_window.__file__).read_text(encoding="utf-8")
        and "before_show" in Path(app_window.__file__).read_text(encoding="utf-8"),
    }


def main():
    print("Evo 窗口的启动画面：网页出来之前不是一块黑")
    checks = {}
    for part in (style_checks, theme_checks, layout_checks, font_checks, draw_checks, api_checks):
        checks.update(part())
    return report(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() is not False else 1)
