"""派喵官网法务文档：运行时从官网拉 Markdown，在 Evo 面板内渲染。

和小程序 `package-more/legal` 的做法一致：
- 内容不打包进客户端，直接请求 `https://apex.skycraft.cn/legal/{agreement,privacy,disclaimer}.md`
  （官网由 `site/build.mjs` 从 ApexPacks 仓库的 `docs/*.md` 生成）。
- 本地缓存 10 分钟；请求失败时优先显示旧缓存，首次无缓存则给重试/浏览器打开。
- 只认白名单里的三份文档，URL 固定；拉回来的文本一律先 HTML 转义再渲染，避免 XSS。

不改上游、不新增依赖：Markdown 渲染用本文件里的最小实现（标题/列表/粗体/链接/代码/引用/分隔线）。
"""

from __future__ import annotations

import html
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from cloud import state as cloud_state

LEGAL_BASE = os.environ.get("EVO_LEGAL_BASE", "https://apex.skycraft.cn/legal/").rstrip("/") + "/"
CACHE_TTL = 10 * 60  # 秒；和小程序一致
TIMEOUT = 10
MAX_BYTES = 256 * 1024

DOCS = {
    "agreement": "用户协议",
    "privacy": "隐私政策",
    "disclaimer": "版权与免责声明",
}


class LegalError(Exception):
    pass


def cache_dir() -> Path:
    folder = cloud_state.data_dir() / "legal"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _cache_path(doc: str) -> Path:
    return cache_dir() / f"{doc}.md"


def _app_version() -> str:
    try:
        import storage  # noqa: PLC0415 上游模块

        return str(getattr(storage, "APP_VERSION", "") or "")
    except Exception:
        return ""


def _read_cache(doc: str):
    """返回 (文本, 缓存了多少秒)；没有缓存返回 (None, None)。"""
    path = _cache_path(doc)
    try:
        text = path.read_text(encoding="utf-8")
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None, None
    if not text.strip():
        return None, None
    return text, age


def _write_cache(doc: str, text: str) -> None:
    try:
        _cache_path(doc).write_text(text, encoding="utf-8")
    except OSError:
        pass  # 缓存失败不影响本次显示


def _fetch(doc: str) -> str:
    url = LEGAL_BASE + doc + ".md"
    request = urllib.request.Request(url, headers={
        "User-Agent": f"Evo/{_app_version()}" if _app_version() else "Evo",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise LegalError(f"官网返回 {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise LegalError("连不上官网") from exc
    if len(data) > MAX_BYTES:
        raise LegalError("文档过大")
    text = data.decode("utf-8", "replace")
    if not text.strip():
        raise LegalError("官网返回内容为空")
    return text


def get_doc(doc: str, force: bool = False) -> dict:
    """取一份文档：{doc, title, html, source, from_cache, error}。doc 不在白名单抛 KeyError。"""
    if doc not in DOCS:
        raise KeyError(doc)
    title = DOCS[doc]
    source = LEGAL_BASE + doc + ".md"
    text, age = _read_cache(doc)
    fresh = text is not None and age is not None and age < CACHE_TTL
    from_cache = fresh and not force
    error = ""
    if not from_cache:
        try:
            text = _fetch(doc)
            _write_cache(doc, text)
            from_cache = False
            error = ""
        except LegalError as exc:
            if text is not None:
                from_cache = True
                error = f"更新失败，显示上次缓存：{exc}"
            else:
                return {"doc": doc, "title": title, "html": "", "source": source,
                        "from_cache": False, "error": f"暂时获取不到：{exc}"}
    return {"doc": doc, "title": title, "html": render_markdown(text), "source": source,
            "from_cache": from_cache, "error": error}


# ---------- 最小 Markdown → HTML（先转义，再套有限的标签） ----------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_UL = re.compile(r"^[-*+]\s+(.*)$")
_OL = re.compile(r"^\d+\.\s+(.*)$")
_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")


def _inline(text: str) -> str:
    s = html.escape(text)  # 含引号一起转义，属性里也不会逃逸
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def render_markdown(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    index, total = 0, len(lines)

    def flush_paragraph():
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(x.strip()) for x in paragraph) + "</p>")
            paragraph.clear()

    # 页面自己有标题：去掉文首的一级标题（和小程序的 stripTitle 一致）
    while index < total and not lines[index].strip():
        index += 1
    if index < total and re.match(r"^#\s+", lines[index]):
        index += 1

    while index < total:
        stripped = lines[index].strip()
        if not stripped:
            flush_paragraph()
            index += 1
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue
        if stripped.startswith("```"):
            flush_paragraph()
            index += 1
            block = []
            while index < total and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            out.append("<pre><code>" + html.escape("\n".join(block)) + "</code></pre>")
            continue
        if _HR.match(stripped):
            flush_paragraph()
            out.append("<hr>")
            index += 1
            continue
        if _UL.match(stripped):
            flush_paragraph()
            items = []
            while index < total and _UL.match(lines[index].strip()):
                items.append(_UL.match(lines[index].strip()).group(1))
                index += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        if _OL.match(stripped):
            flush_paragraph()
            items = []
            while index < total and _OL.match(lines[index].strip()):
                items.append(_OL.match(lines[index].strip()).group(1))
                index += 1
            out.append("<ol>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ol>")
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            quoted = []
            while index < total and lines[index].strip().startswith(">"):
                quoted.append(lines[index].strip()[1:].strip())
                index += 1
            out.append("<blockquote>" + "<br>".join(_inline(x) for x in quoted) + "</blockquote>")
            continue
        paragraph.append(stripped)
        index += 1

    flush_paragraph()
    return "\n".join(out)
