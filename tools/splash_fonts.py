"""取 Evo 窗口启动画面上那行字用的字体（开发工具）：python tools/splash_fonts.py

启动画面（code/splash.py）的「APEX 爱好家」：APEX 用 Cinzel，爱好家用思源宋体（Noto Serif SC）ExtraLight，
都是 OFL 许可（assets/splash/OFL.txt）。从 Google Fonts 只取要用的那几个字（text= 参数，每个文件几 KB），
存进 assets/splash/（文件名照 splash.TAGLINE），随安装包发。改了 TAGLINE 里的字就重新运行一次，再提交。
"""

import re
import sys
import urllib.parse
import urllib.request

import code_path  # noqa: F401  让下面能 import 程序代码

import splash

FAMILIES = ["Cinzel", "Noto Serif SC:wght@200"]  # Google Fonts 上的名字和粗细，顺序和 splash.TAGLINE 一样


def fetch(family, text):
    query = urllib.parse.urlencode({"family": family, "text": text})
    # 不带浏览器的 User-Agent 时，Google Fonts 给 TTF（PIL 直接能读）
    css = urllib.request.urlopen(f"https://fonts.googleapis.com/css2?{query}", timeout=30).read().decode()
    url = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", css).group(1)
    return urllib.request.urlopen(url, timeout=30).read()


def main():
    splash.FONT_DIR.mkdir(parents=True, exist_ok=True)
    for family, (text, filename, *_rest) in zip(FAMILIES, splash.TAGLINE, strict=True):
        data = fetch(family, text)
        if data[:4] not in (b"\x00\x01\x00\x00", b"true"):
            sys.exit(f"{family}：下下来的不是 TTF")
        (splash.FONT_DIR / filename).write_bytes(data)
        print(f"{filename}：{text}，{len(data)} 字节")


if __name__ == "__main__":
    main()
