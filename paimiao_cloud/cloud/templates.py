"""Evo 派喵云同步 · 模板加载。

只做一件事：在 Evo 自己的模板目录后面加一个"我们的目录"，
这样 `render_template("cloud_page.html")` 能找到我们自己的页面。

**不改 settings.html、不拼接任何字符串** —— 设置页保持上游原样，
派喵云同步是一个独立页面（导航栏第 4 项，见 cloud/panel.py 的 nav_items）。
"""
from __future__ import annotations

from jinja2 import ChoiceLoader, FileSystemLoader


def splice_loader(existing, cloud_templates_dir):
    """Evo 的 loader 优先，找不到的模板再找我们自己的目录。

    名字保留 splice_loader 是为了不破坏历史调用点；现在它只做叠加，不做拼接。
    """
    ours = FileSystemLoader(cloud_templates_dir)
    if existing is None:
        return ours
    return ChoiceLoader([existing, ours])
