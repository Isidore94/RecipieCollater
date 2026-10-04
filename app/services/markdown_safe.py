"""Tiny, allowlist-only Markdown to HTML for assistant replies.

Model output is untrusted. This renderer never passes input through: every character is
HTML-escaped first, then a fixed set of constructs is re-introduced as tags the renderer itself
emits (p, br, ul, ol, li, strong, em, code, a). There is no raw-HTML pass-through, so a
``<script>`` tag is shown as inert text, and links are only emitted for http(s) URLs
(``rel="noopener noreferrer"``, ``target="_blank"``); any other scheme such as ``javascript:``
keeps just its label. Headings are flattened to bold paragraphs: the page already has an h1 and
a chat bubble is not a document.
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup

_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s{0,3}\d{1,3}[.)]\s+(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^\s()]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC_STAR = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])")
_ITALIC_UNDER = re.compile(r"(?<![_\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![_\w])")
_STASH = "\x00{}\x00"


def _inline(raw: str) -> str:
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return _STASH.format(len(stash) - 1)

    text = html.escape(raw.replace("\x00", ""), quote=True)
    text = _CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), text)

    def link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        if not url.lower().startswith(("http://", "https://")):
            return label  # javascript:, data:, relative... keep the words, drop the link
        return keep(f'<a href="{url}" rel="noopener noreferrer" target="_blank">{label}</a>')

    text = _LINK.sub(link, text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(2)}</strong>", text)
    text = _ITALIC_STAR.sub(lambda m: f"<em>{m.group(1)}</em>", text)
    text = _ITALIC_UNDER.sub(lambda m: f"<em>{m.group(1)}</em>", text)
    for i, fragment in enumerate(stash):
        text = text.replace(_STASH.format(i), fragment)
    return text


def render(text: str | None) -> Markup:
    """Render *text* as safe HTML (see module docstring for the allowlist)."""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag = ""

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if items:
            out.append(
                f"<{list_tag}>"
                + "".join(f"<li>{_inline(i)}</li>" for i in items)
                + f"</{list_tag}>"
            )
            items.clear()
        list_tag = ""

    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        bullet, ordered, heading = _BULLET.match(line), _ORDERED.match(line), _HEADING.match(line)
        if not line.strip():
            flush_para()
            flush_list()
        elif heading:
            flush_para()
            flush_list()
            out.append(f"<p><strong>{_inline(heading.group(1))}</strong></p>")
        elif bullet or ordered:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            match = bullet or ordered
            assert match is not None
            items.append(match.group(1))
        else:
            flush_list()
            para.append(line.strip())
    flush_para()
    flush_list()
    return Markup("".join(out))
