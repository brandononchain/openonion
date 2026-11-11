"""Turn a web page into compact, model-friendly text.

Raw page text overwhelms the context window: navigation, cookie banners,
footers, share widgets and inline scripts often outweigh the content. This
module is the "browser-to-LLM bridge" shared by `web_fetch`, `browse` and
`tor_browse`:

1. Parse the HTML into a small tree (stdlib only; nothing extra goes over Tor).
2. Drop non-content subtrees: scripts, styles, SVG, iframes, forms, nav,
   headers/footers/asides outside the main content, ARIA-hidden or `hidden`
   elements, inline `display:none`, and common boilerplate by class/id
   (cookie banners, modals, newsletter prompts, share bars, ads).
3. Keep only the main content (`<main>`, `role=main`, or `<article>`) when the
   page marks one.
4. Render it as light markdown: headings, lists, tables, quotes, code. Links
   become short references (`text [L3]`); `paginate` lists the URLs that each
   chunk mentions under it, so long URLs never repeat inline.

Hidden text is a classic prompt-injection channel, so dropping it is also a
safety measure. The browser removes elements hidden by computed CSS before it
hands the DOM over (see `BrowserService.visit`); this module handles the
markup-level signals for both paths.

`paginate` slices long results so the agent can read on with `offset`
instead of losing everything past a hard cutoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

# Never content, wherever they appear.
DROP_TAGS = {
    "script", "style", "noscript", "template", "svg", "math", "canvas", "iframe", "object", "embed",
    "head", "nav", "form", "button", "select", "option", "textarea", "input", "label", "dialog",
    "audio", "video", "picture", "map", "menu",
}
DROP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alertdialog",
              "menu", "menubar", "toolbar", "tooltip"}
# Site chrome: dropped when outside the chosen main content.
CHROME_TAGS = {"header", "footer", "aside"}
BOILERPLATE = re.compile(
    r"(^|[\s_-])(cookie|consent|gdpr|banner|modal|popup|overlay|newsletter|subscribe|signup|"
    r"advert|ads?|sponsor|promo|share|social|breadcrumbs?|skip-link|sr-only|visually-hidden|"
    r"sidebar|related|recommend|paywall)([\s_-]|$)",
    re.I,
)
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)

BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "figure", "figcaption",
         "address", "details", "summary", "dl", "dt", "dd", "center", "body", "html", "hgroup"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

MAX_LINKS = 60


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node | str"] = field(default_factory=list)
    parent: "_Node | None" = None


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.cur = self.root
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        node = _Node(tag, {k: (v or "") for k, v in attrs}, parent=self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, {k: (v or "") for k, v in attrs}, parent=self.cur))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        # Close up to the nearest matching open element; ignore stray end tags.
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        self.cur.children.append(data)


def _is_dropped(node: _Node, in_main: bool, heuristics: bool = True) -> bool:
    if node.tag in DROP_TAGS:
        return True
    a = node.attrs
    if "hidden" in a or a.get("aria-hidden", "").lower() == "true" or a.get("aria-modal", "").lower() == "true":
        return True
    if a.get("role", "").lower() in DROP_ROLES:
        return True
    if HIDDEN_STYLE.search(a.get("style", "")):
        return True
    if not in_main and node.tag in CHROME_TAGS:
        return True
    marker = f"{a.get('class', '')} {a.get('id', '')}"
    if heuristics and marker.strip() and node.tag not in {"main", "article", "body", "html"} and BOILERPLATE.search(marker):
        return True
    return False


def _find_main(root: _Node) -> _Node | None:
    """First <main> / role=main, else the single <article> (several articles = a listing; keep the body)."""
    articles: list[_Node] = []
    stack = [root]
    while stack:
        n = stack.pop(0)
        if n.tag == "main" or n.attrs.get("role", "").lower() == "main":
            return n
        if n.tag == "article":
            articles.append(n)
        stack.extend(c for c in n.children if isinstance(c, _Node))
    return articles[0] if len(articles) == 1 else None


class _Renderer:
    def __init__(self, base_url: str, links: bool, heuristics: bool = True) -> None:
        self.base_url = base_url
        self.heuristics = heuristics
        self.want_links = links
        self.out: list[str] = []
        self.links: list[str] = []
        self._link_ids: dict[str, int] = {}
        self.list_depth = 0
        self.pre = 0

    # ── output helpers ───────────────────────────────────────────────────────
    def text(self, s: str) -> None:
        if self.pre:
            self.out.append(s)
            return
        s = re.sub(r"\s+", " ", s)
        if not self.out or self.out[-1].endswith("\n"):
            s = s.lstrip()
        if s.strip() or (s == " " and self.out and not self.out[-1].endswith((" ", "\n"))):
            self.out.append(s)

    def block(self, n: int = 2) -> None:
        tail = "".join(self.out[-3:])
        have = len(tail) - len(tail.rstrip("\n"))
        if self.out and have < n:
            self.out.append("\n" * (n - have))

    def _link_ref(self, href: str) -> int | None:
        href = href.strip()
        if not href or href.startswith(("#", "javascript:", "data:", "vbscript:")):
            return None
        url = urljoin(self.base_url, href) if self.base_url else href
        if urlsplit(url).scheme not in {"http", "https", "mailto", ""}:
            return None
        if url in self._link_ids:
            return self._link_ids[url]
        if len(self.links) >= MAX_LINKS:
            return None
        self.links.append(url)
        self._link_ids[url] = len(self.links)
        return len(self.links)

    # ── tree walk ────────────────────────────────────────────────────────────
    def render(self, node: _Node, in_main: bool) -> None:
        for child in node.children:
            if isinstance(child, str):
                self.text(child)
            elif not _is_dropped(child, in_main, self.heuristics):
                self.element(child, in_main)

    def inline_text(self, node: _Node) -> str:
        sub = _Renderer(self.base_url, links=False, heuristics=self.heuristics)
        sub.render(node, in_main=True)
        return re.sub(r"\s+", " ", "".join(sub.out)).strip()

    def element(self, n: _Node, in_main: bool) -> None:
        t = n.tag
        if t in HEADINGS:
            self.block()
            self.out.append("#" * HEADINGS[t] + " ")
            self.render(n, in_main)
            self.block()
        elif t in {"ul", "ol"}:
            self.block(1 if self.list_depth else 2)
            self.list_depth += 1
            for i, li in enumerate((c for c in n.children if isinstance(c, _Node) and c.tag == "li"), 1):
                if _is_dropped(li, in_main, self.heuristics):
                    continue
                self.block(1)
                self.out.append("  " * (self.list_depth - 1) + (f"{i}. " if t == "ol" else "- "))
                self.render(li, in_main)
            self.list_depth -= 1
            self.block(1 if self.list_depth else 2)
        elif t == "li":  # stray <li> outside a list
            self.block(1)
            self.out.append("- ")
            self.render(n, in_main)
        elif t == "br":
            self.out.append("\n")
        elif t == "hr":
            self.block()
            self.out.append("---")
            self.block()
        elif t == "pre":
            self.block()
            self.out.append("```\n")
            self.pre += 1
            self.render(n, in_main)
            self.pre -= 1
            self.block(1)
            self.out.append("```")
            self.block()
        elif t == "code" and not self.pre:
            self.out.append(f"`{self.inline_text(n)}`")
        elif t == "blockquote":
            self.block()
            inner = _Renderer(self.base_url, self.want_links, self.heuristics)
            inner.links, inner._link_ids = self.links, self._link_ids
            inner.render(n, in_main)
            body = _tidy("".join(inner.out))
            self.out.append("\n".join("> " + line if line else ">" for line in body.splitlines()))
            self.block()
        elif t == "table":
            self.table(n, in_main)
        elif t in {"strong", "b"}:
            inner = self.inline_text(n)
            if inner:
                self.out.append(f"**{inner}**")
        elif t in {"em", "i"}:
            inner = self.inline_text(n)
            if inner:
                self.out.append(f"*{inner}*")
        elif t == "a":
            start = len(self.out)
            self.render(n, in_main)
            label = "".join(self.out[start:]).strip()
            href = n.attrs.get("href", "")
            if href.startswith("#") and re.fullmatch(r"\[?\d{1,3}\]?", label):
                del self.out[start:]  # in-page footnote markers like [12] are noise
                return
            ref = self._link_ref(href) if self.want_links and label else None
            if ref is not None:
                self.out.append(f" [L{ref}]")
        elif t == "img":
            alt = n.attrs.get("alt", "").strip()
            if alt:
                self.out.append(f"[image: {alt}]")
        elif t in BLOCK or t in {"tr", "tbody", "thead"}:
            self.block()
            self.render(n, in_main)
            self.block()
        else:
            self.render(n, in_main)

    def table(self, n: _Node, in_main: bool) -> None:
        rows: list[list[str]] = []
        stack = [n]
        while stack:
            cur = stack.pop(0)
            for c in cur.children:
                if not isinstance(c, _Node) or _is_dropped(c, in_main, self.heuristics):
                    continue
                if c.tag == "tr":
                    cells = [self.inline_text(td).replace("|", "\\|") for td in c.children
                             if isinstance(td, _Node) and td.tag in {"td", "th"}]
                    if any(cells):
                        rows.append(cells)
                elif c.tag != "table":
                    stack.append(c)
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        self.block()
        self.out.append("| " + " | ".join(rows[0]) + " |\n|" + "---|" * width)
        for r in rows[1:]:
            self.out.append("\n| " + " | ".join(r) + " |")
        self.block()


def _tidy(s: str) -> str:
    """Collapse stray spaces and blank-line runs; keep indentation and code blocks intact."""
    lines: list[str] = []
    fence = False
    for line in s.strip("\n").splitlines():
        if line.strip() == "```":
            fence = not fence
        elif not fence:
            line = re.sub(r"(?<=\S)[ \t]{2,}", " ", line)
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@dataclass
class Readable:
    title: str
    text: str
    links: list[str]


def simplify_html(html: str, base_url: str = "", *, links: bool = True) -> Readable:
    """Extract the readable content of `html` as compact markdown."""
    tb = _TreeBuilder()
    tb.feed(html)
    tb.close()
    main = _find_main(tb.root)

    def render(heuristics: bool) -> tuple[str, list[str]]:
        r = _Renderer(base_url, links, heuristics)
        r.render(main or tb.root, in_main=main is not None)
        return _tidy("".join(r.out)), r.links

    text, found = render(True)
    if len(text) < 500:
        # The class/id boilerplate guesses can misfire on a page-wide wrapper
        # (e.g. <div class="has-sidebar">). If they removed most of the page, drop them.
        lenient, lenient_links = render(False)
        if len(lenient) > 4 * max(len(text), 100):
            text, found = lenient, lenient_links
    return Readable(title=re.sub(r"\s+", " ", tb.title).strip(), text=text, links=found)


_REF = re.compile(r"\[L(\d+)\]")


def paginate(text: str, offset: int = 0, limit: int = 12_000, links: list[str] | None = None) -> str:
    """Return one window of `text`, with a footer telling the model how to read on.

    Cuts on a paragraph or line break near the limit so blocks aren't split mid-sentence.
    With `links`, appends the URLs for the `[Ln]` references that appear in this window.
    """
    total = len(text)
    if offset >= total and total:
        return f"[offset {offset} is past the end; the page has {total} characters]"
    end = min(total, offset + limit)
    if end < total:
        for sep in ("\n\n", "\n", ". "):
            cut = text.rfind(sep, offset, end)
            if cut > offset + limit // 2:
                end = cut + len(sep)
                break
    chunk = text[offset:end].strip("\n")
    if links:
        refs = sorted({int(m) for m in _REF.findall(chunk) if 0 < int(m) <= len(links)})
        if refs:
            chunk += "\n\nLinks:\n" + "\n".join(f"[L{i}] {links[i - 1]}" for i in refs)
    if offset == 0 and end == total:
        return chunk
    note = f"[characters {offset}–{end} of {total}"
    note += f"; call again with offset={end} to continue]" if end < total else "; end of page]"
    return chunk + "\n\n" + note
