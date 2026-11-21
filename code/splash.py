"""Evo 窗口的启动画面：窗口刚出来、网页还没画好的那一两秒，显示标题栏、图标、「APEX 爱好者」和一条来回走的细加载条，不是一块黑。

- 为什么会有这一两秒：窗口进程打开窗口以后，WebView2（Edge 内核）要先启动自己的浏览器进程，再加载、画出面板网页
  （2026-11-21 量过：源码运行时窗口出来以后约 0.5 秒，打包版、电脑忙的时候更久）
- 用 WinForms 自己画（app_window.run 在窗口显示之前盖上），不靠 WebView2，窗口一出来就有；网页画好了
  （base.html 调 WindowApi.page_shown）就拿掉，标题栏和网页里的一样，看起来只是中间换成了内容。
  网页一直没来调（出错页等）：加载完 2 秒后也拿掉；最多盖 15 秒
- 颜色跟着面板的主题：网页把现在选的主题告诉窗口（WindowApi.remember_theme，记在 settings.json 的 window_theme），
  下次打开时用它；没记过或选的是"跟随系统"，看 Windows 的深浅色设置
- 那行字（TAGLINE，2026-11-22 用户定的样子和字体）：APEX 用 Cinzel，爱好者用思源宋体（Noto Serif SC）ExtraLight，
  assets/splash/ 里是只含这几个字的小字体文件（OFL 许可，python tools/splash_fonts.py 从 Google Fonts 取，改字以后重新取）；
  标题栏的 EVO 用面板自己的 Chakra Petch
"""

import ctypes
import io
import math
import time
import winreg
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageFont

import icons
from paths import ASSETS_DIR, PANEL_DIR

THEME_CHOICES = ("dark", "light", "system")  # 和 base.html 的 evoTheme() 一样
# 和 panel/static/style.css 里两个主题的颜色一样
THEMES = {
    "dark": {"bg": "#0D0F13", "titlebar": "#08090C", "border": "#262C38", "muted": "#9AA3B2", "track": "#1B1F29"},
    "light": {"bg": "#F6F6F3", "titlebar": "#EAEAE5", "border": "#E4E4DF", "muted": "#5F6670", "track": "#E7E7E2"},
}
BAR_COLOR = "#3B9EFF"  # 图标最上面那一条的蓝色

FONT_DIR = ASSETS_DIR / "splash"
# 下面的大小都是网页里的像素（style.css），画的时候按 Windows 的缩放比例放大
TAGLINE = [  # (字, 字体文件, 字号, 字间距)；改了字要重新运行 tools/splash_fonts.py
    ("APEX", "Cinzel-tagline.ttf", 20, 5),
    ("爱好者", "NotoSerifSC-ExtraLight-tagline.ttf", 20, 4),
]
TAGLINE_GAP = 10  # APEX 和爱好者之间
ICON = 96
GAP = 24  # 图标、字、加载条之间
BAR_W, BAR_H = 110, 2
BAR_SEGMENT = 0.4  # 走动的那一段占整条的多少
BAR_PERIOD = 1.1  # 从左走到右一趟几秒
TITLEBAR_H = 34  # 含下边框 1 像素（.titlebar）
TITLE_PAD, TITLE_ICON, TITLE_GAP = 12, 18, 8
TITLE_FONT = (PANEL_DIR / "static" / "fonts" / "ChakraPetch-700.woff2", 12, 2)  # .titlebar-name：字体、字号、字间距
WIN_BTN_W, WIN_ICON = 46, 10  # .win-btn 和里面的小图标
FALLBACK_AFTER_LOAD = 2.0  # 网页加载完了却一直没调 page_shown：几秒后也拿掉
GIVE_UP = 15.0  # 最多盖这么久

SPI_GETCLIENTAREAANIMATION = 0x1042
_user32 = ctypes.WinDLL("user32")
_user32.GetDpiForWindow.argtypes = [wintypes.HWND]
_user32.GetDpiForWindow.restype = wintypes.UINT


def windows_light():
    """Windows 设置里"应用"用的是浅色吗（WebView2 的"跟随系统"看的也是它）。读不到就当深色。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 1
    except OSError:
        return False


def resolve_theme(choice, light=None):
    """面板里选的主题（dark / light / system，没记过是 None）→ 这次用 "dark" 还是 "light"。"""
    if choice in ("dark", "light"):
        return choice
    return "light" if (windows_light() if light is None else light) else "dark"


def animations_on():
    """Windows 设置里没关"动画效果"（关了就不让加载条走，和网页的 prefers-reduced-motion 一样）。"""
    on = wintypes.BOOL(True)
    if not _user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(on), 0):
        return True
    return bool(on.value)


def text_image(parts, scale, color, gap=0):
    """几段字画成一张透明底的图，各段基线对齐。parts：[(字, 字体文件, 字号, 字间距)]，大小按网页像素 × scale。"""
    runs = [(text, ImageFont.truetype(str(path), max(1, round(size * scale))), spacing * scale)
            for text, path, size, spacing in parts]
    ascent = max(font.getmetrics()[0] for _, font, _ in runs)
    descent = max(font.getmetrics()[1] for _, font, _ in runs)
    places, x = [], 0.0
    for i, (text, font, spacing) in enumerate(runs):
        if i:
            x += gap * scale
        for j, ch in enumerate(text):
            places.append((x, ch, font))
            x += font.getlength(ch) + (spacing if j < len(text) - 1 else 0)
    img = Image.new("RGBA", (max(1, math.ceil(x)), ascent + descent), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for x0, ch, font in places:
        draw.text((x0, ascent), ch, font=font, fill=color, anchor="ls")
    return img


def bar_segment(elapsed, width, segment=BAR_SEGMENT, period=BAR_PERIOD):
    """加载条上走动的那一段：返回 (左, 右)，相对于整条的左端，已经截在条里；这一刻正好在条外面时返回 None。"""
    p = (elapsed % period) / period
    eased = p * p * (3 - 2 * p)  # 慢 - 快 - 慢
    seg = width * segment
    left = -seg + eased * (width + seg)
    a, b = max(0, round(left)), min(width, round(left + seg))
    return (a, b) if b > a else None


def layout(width, height, scale, sizes):
    """启动画面上各样东西的位置（窗口里的实际像素）。sizes：{"icon": 边长, "tagline": (宽, 高)}。
    返回 {"titlebar": 高, "line": 边框粗细, "icon": (x, y), "tagline": (x, y), "bar": (x, y, 宽, 高), "buttons": [左边的 x…]}"""
    th, line = round(TITLEBAR_H * scale), max(1, int(scale))
    icon, (tw, tag_h) = sizes["icon"], sizes["tagline"]
    gap, bw, bh = round(GAP * scale), round(BAR_W * scale), max(1, round(BAR_H * scale))
    top = th + (height - th - (icon + gap + tag_h + gap + bh)) // 2  # 一组东西在标题栏下面的地方上下居中
    btn = round(WIN_BTN_W * scale)
    return {
        "titlebar": th, "line": line,
        "icon": ((width - icon) // 2, top),
        "tagline": ((width - tw) // 2, top + icon + gap),
        "bar": ((width - bw) // 2, top + icon + gap + tag_h + gap, bw, bh),
        "buttons": [width - 3 * btn, width - 2 * btn, width - btn],
    }


def _bitmap(img):
    """PIL 的图 → WinForms 用的 Bitmap。"""
    from System import Array, Byte
    from System.Drawing import Bitmap
    from System.IO import MemoryStream

    buf = io.BytesIO()
    img.save(buf, "PNG")
    stream = MemoryStream(Array[Byte](buf.getvalue()))
    loaded = Bitmap(stream)
    bmp = Bitmap(loaded)  # 复制一份：从流里读出来的图片要一直留着那个流
    loaded.Dispose()
    stream.Dispose()
    return bmp


class Splash:
    """盖在窗口上的启动画面。在窗口自己的线程里、窗口显示之前创建（app_window.run 的 before_show）；remove() 哪个线程都能调。"""

    def __init__(self, form, theme):
        from System.Drawing import ColorTranslator, SolidBrush
        from System.Windows.Forms import DockStyle, PictureBox
        from System.Windows.Forms import Timer as UiTimer

        self.form, self.colors = form, THEMES[theme]
        self.scale = (_user32.GetDpiForWindow(form.Handle.ToInt64()) or 96) / 96
        self.animate = animations_on()
        self._t0 = time.perf_counter()
        self._gone = False
        self._bar = None  # 加载条在窗口里的位置，动的时候只重画这一块

        s, muted = self.scale, self.colors["muted"]
        big = icons.base_icon(256)[0]  # 原图 1024 像素，读一次约 20 毫秒（窗口会晚出来这么久）：只读一次，再缩成两种大小
        logo = big.resize((round(ICON * s),) * 2, Image.LANCZOS)
        title_icon = big.resize((round(TITLE_ICON * s),) * 2, Image.LANCZOS)
        font, size, spacing = TITLE_FONT
        self.images = {
            "icon": _bitmap(logo),
            "title_icon": _bitmap(title_icon),
            "title": _bitmap(text_image([("EVO", font, size, spacing)], s, muted)),
            "tagline": _bitmap(text_image([(t, FONT_DIR / f, z, sp) for t, f, z, sp in TAGLINE], s, muted, TAGLINE_GAP)),
        }
        self.brushes = {name: SolidBrush(ColorTranslator.FromHtml(value))
                        for name, value in {**self.colors, "accent": BAR_COLOR}.items()}

        # PictureBox：自带双缓冲，加载条动起来不闪。但双缓冲的控件第一次画好之前那一块是空的，窗口刚出来时会白一下
        # （2026-11-22 截图看到约 30–60 毫秒）：先让它像普通控件一样立刻用底色铺满背景，第一次画完再换回去（_paint）
        self.box = PictureBox()
        self.box.Dock = DockStyle.Fill
        self.box.BackColor = ColorTranslator.FromHtml(self.colors["bg"])
        self._erase_first(True)
        self.box.Paint += self._paint
        form.Controls.Add(self.box)
        self.box.BringToFront()  # 盖在 WebView2 上面
        self.timer = UiTimer()
        self.timer.Interval = 15
        self.timer.Tick += self._tick
        self.timer.Start()

    def _erase_first(self, on):
        """on=True：Windows 一要求就先用底色铺背景（关掉 AllPaintingInWmPaint）；False：恢复 PictureBox 原来的双缓冲画法。
        SetStyle 是 WinForms 不公开的方法，用反射调。"""
        import clr
        from System import Array, Object
        from System.Reflection import BindingFlags
        from System.Windows.Forms import Control, ControlStyles

        method = clr.GetClrType(Control).GetMethod("SetStyle", BindingFlags.NonPublic | BindingFlags.Instance)
        method.Invoke(self.box, Array[Object]([ControlStyles.AllPaintingInWmPaint, not on]))
        self._erasing = on

    def _draw(self, g, img, x, y):
        from System.Drawing import Rectangle

        g.DrawImage(img, Rectangle(x, y, img.Width, img.Height))  # 按像素原样画，不按图片的 DPI 缩放

    def _paint(self, sender, e):
        from System.Drawing import Pen
        from System.Drawing.Drawing2D import SmoothingMode
        from System.Windows.Forms import FormWindowState

        g, b, s = e.Graphics, self.brushes, self.scale
        w, h = self.box.ClientSize.Width, self.box.ClientSize.Height
        im = self.images
        at = layout(w, h, s, {"icon": im["icon"].Width, "tagline": (im["tagline"].Width, im["tagline"].Height)})
        th, line = at["titlebar"], at["line"]
        inner = th - line  # 标题栏去掉下边框，图标和字在这里面上下居中

        # 标题栏：和 base.html 的 .titlebar 一样
        g.FillRectangle(b["titlebar"], 0, 0, w, th)
        g.FillRectangle(b["border"], 0, th - line, w, line)
        x = round(TITLE_PAD * s)
        self._draw(g, im["title_icon"], x, (inner - im["title_icon"].Height) // 2)
        self._draw(g, im["title"], x + im["title_icon"].Width + round(TITLE_GAP * s), (inner - im["title"].Height) // 2)
        icon, bw = round(WIN_ICON * s), round(WIN_BTN_W * s)
        top = (inner - icon) // 2
        lefts = [x0 + (bw - icon) // 2 for x0 in at["buttons"]]
        g.FillRectangle(b["muted"], lefts[0], top + (icon - line) // 2, icon, line)  # 最小化：一横
        for rect in ((0, 0, icon, line), (0, icon - line, icon, line), (0, 0, line, icon), (icon - line, 0, line, icon)):
            g.FillRectangle(b["muted"], lefts[1] + rect[0], top + rect[1], rect[2], rect[3])  # 最大化：方框
        pen = Pen(b["muted"].Color, line)
        g.SmoothingMode = SmoothingMode.AntiAlias
        g.DrawLine(pen, lefts[2], top, lefts[2] + icon, top + icon)  # 关闭：叉
        g.DrawLine(pen, lefts[2] + icon, top, lefts[2], top + icon)
        g.SmoothingMode = SmoothingMode.Default
        pen.Dispose()

        # 中间：图标、字、加载条
        self._draw(g, im["icon"], *at["icon"])
        self._draw(g, im["tagline"], *at["tagline"])
        bx, by, bar_w, bar_h = self._bar = at["bar"]
        g.FillRectangle(b["track"], bx, by, bar_w, bar_h)
        part = bar_segment(time.perf_counter() - self._t0, bar_w) if self.animate else None
        if part:
            g.FillRectangle(b["accent"], bx + part[0], by, part[1] - part[0], bar_h)

        # 窗口四周的细边（.window-frame，最大化时没有）
        if self.form.WindowState != FormWindowState.Maximized:
            for rect in ((0, 0, w, line), (0, h - line, w, line), (0, 0, line, h), (w - line, 0, line, h)):
                g.FillRectangle(b["border"], *rect)
        if self._erasing:  # 第一次画好了，换回双缓冲（见 __init__）
            self._erase_first(False)

    def _tick(self, sender, e):
        from System.Drawing import Rectangle

        if self._gone:
            return
        if time.perf_counter() - self._t0 > GIVE_UP:
            self.remove()
        elif self.animate and self._bar:
            self.box.Invalidate(Rectangle(*self._bar))

    def remove(self):
        """拿掉启动画面，露出网页。可以调好几次。"""
        if self._gone:
            return
        self._gone = True
        from System import Action

        try:
            self.form.BeginInvoke(Action(self._remove_now))
        except Exception:  # noqa: BLE001 窗口已经关了
            pass

    def _remove_now(self):
        self.timer.Stop()
        self.timer.Dispose()
        self.form.Controls.Remove(self.box)
        self.box.Dispose()
        for item in (*self.images.values(), *self.brushes.values()):
            item.Dispose()
