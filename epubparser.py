"""
EPUB parsing library — extracts chapters, covers, and plain text
from EPUB files.  Audiobook-oriented: focuses on linear reading order
and clean text suitable for TTS synthesis.
"""

import posixpath
import re
from typing import Any, Dict, List, Optional, Set

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, ProcessingInstruction, Tag
from ebooklib import ITEM_DOCUMENT, ITEM_IMAGE, epub

# ══════════════════════════════════════════════════════════════
# Path helpers
# ══════════════════════════════════════════════════════════════

def _norm(p: str) -> str:
    return posixpath.normpath(p).lstrip("./")

def _resolve(base_href: str, rel_href: str) -> str:
    base_dir = posixpath.dirname(base_href)
    return _norm(posixpath.join(base_dir, rel_href))

# ══════════════════════════════════════════════════════════════
# EPUB structure helpers
# ══════════════════════════════════════════════════════════════

def _is_linear(meta: Any) -> bool:
    if meta is None:
        return True
    if isinstance(meta, str):
        return meta.strip().lower() != "no"
    if isinstance(meta, dict):
        val = (meta.get("linear", "yes") or "yes").strip().lower()
        return val != "no"
    return True

def _collect_toc_hrefs(book: epub.EpubBook) -> Set[str]:
    toc_hrefs: Set[str] = set()
    for it in book.get_items():
        if isinstance(it, epub.EpubNav):
            toc_hrefs.add(_norm(it.get_name()))
    for it in book.get_items_of_type(ITEM_DOCUMENT):
        try:
            soup = BeautifulSoup(it.get_content(), features='xml')
        except Exception:
            continue
        for n in soup.find_all("nav"):
            et = (n.attrs.get("epub:type") or n.attrs.get("type") or "")
            role = n.attrs.get("role") or ""
            et_tokens = {t.strip() for t in et.split()} if et else set()
            role_tokens = {t.strip() for t in role.split()} if role else set()
            if ("toc" in et_tokens) or ("doc-toc" in role_tokens):
                toc_hrefs.add(_norm(it.get_name()))
                break
    guide = getattr(book, "guide", None)
    if guide:
        for g in guide:
            try:
                if (g.get("type") or "").lower() == "toc" and g.get("href"):
                    toc_hrefs.add(_norm(g["href"]))
            except Exception:
                if isinstance(g, (list, tuple)) and len(g) >= 2:
                    g_type = (g[0] or "").lower()
                    g_href = g[1] if len(g) > 1 else None
                    if g_type == "toc" and g_href:
                        toc_hrefs.add(_norm(str(g_href)))
    return toc_hrefs

# ══════════════════════════════════════════════════════════════
# Text extraction
# ══════════════════════════════════════════════════════════════

_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body",
    "caption", "center", "dd", "details", "dialog", "dir", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "html",
    "legend", "li", "main", "menu", "nav",
    "ol", "p", "pre", "section", "summary",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "ul",
})
_SKIP_TAGS = frozenset({"script", "style", "head"})
_RE_HSPACE = re.compile(r"[ \t]+")
_RE_MULTI_NL = re.compile(r"\n[ \t]*\n+")

def _extract_text(soup: BeautifulSoup) -> str:
    parts: List[str] = []
    def _walk(node):
        if isinstance(node, (Comment, Doctype, ProcessingInstruction)):
            return
        if isinstance(node, NavigableString):
            text = str(node)
            text = _RE_HSPACE.sub(" ", text.replace("\n", " "))
            if text:
                parts.append(text)
            return
        if not isinstance(node, Tag):
            return
        tag = (node.name or "").lower()
        if tag in _SKIP_TAGS:
            return
        if tag == "br":
            parts.append("\n")
            return
        is_block = tag in _BLOCK_TAGS
        if is_block:
            parts.append("\n")
        for child in node.children:
            _walk(child)
        if is_block:
            parts.append("\n")
    start = soup.find("body") or soup
    _walk(start)
    raw = "".join(parts)
    raw = _RE_MULTI_NL.sub("\n", raw)
    lines = [_RE_HSPACE.sub(" ", ln).strip() for ln in raw.split("\n")]
    return "\n".join(ln for ln in lines if ln)

def _find_image_by_href(book, href):
    target = _norm(href)
    for img in book.get_items_of_type(ITEM_IMAGE):
        if _norm(img.get_name()) == target:
            return img
    base = posixpath.basename(target)
    for img in book.get_items_of_type(ITEM_IMAGE):
        if posixpath.basename(img.get_name()) == base:
            return img
    return None

def _soup_title_like(soup):
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    h = soup.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if h:
        return h.get_text(strip=True)
    return None

# ══════════════════════════════════════════════════════════════
# Chapter / cover extraction
# ══════════════════════════════════════════════════════════════

def extract_chapters(book: epub.EpubBook) -> List[Dict[str, Any]]:
    id_to_item = {it.get_id(): it for it in book.get_items()}
    spine = [
        (entry[0], entry[1] if len(entry) > 1 else None)
        for entry in book.spine
        if isinstance(entry, tuple) and entry
    ]
    spine_ids = [sid for sid, _ in spine]
    id_to_spine_meta = {sid: meta for sid, meta in spine}
    toc_hrefs = _collect_toc_hrefs(book)
    chapters = []
    for idx, cid in enumerate(spine_ids):
        it = id_to_item.get(cid)
        if not it or it.get_type() != ITEM_DOCUMENT:
            continue
        href = _norm(it.get_name())
        if href in toc_hrefs:
            continue
        raw = it.get_content()
        soup = BeautifulSoup(raw, features='xml')
        title = _soup_title_like(soup) or cid
        text = _extract_text(soup)
        chapters.append({
            "id": cid, "spine_index": idx,
            "linear": _is_linear(id_to_spine_meta.get(cid)),
            "title": title, "text": text,
            "word_count": len(text.split()),
        })
    return chapters


def get_cover(book: epub.EpubBook) -> Optional[Dict[str, Any]]:
    for it in book.get_items():
        if isinstance(it, epub.EpubCover):
            mt = getattr(it, "media_type", None)
            if mt and mt.startswith("image/"):
                return {"filename": it.get_name(), "bytes": it.get_content(), "mime": mt}
            if mt and mt.startswith("text/"):
                try:
                    soup = BeautifulSoup(it.get_content(), features='xml')
                    img = soup.find("img", src=True)
                    if img:
                        base = posixpath.dirname(it.get_name())
                        rel = img["src"]
                        href = rel if rel.startswith("/") else posixpath.normpath(posixpath.join(base, rel))
                        for img_item in book.get_items_of_type(ITEM_IMAGE):
                            if posixpath.normpath(img_item.get_name()) == href:
                                return {"filename": img_item.get_name(), "bytes": img_item.get_content(), "mime": img_item.media_type}
                except Exception:
                    pass
    metas = book.get_metadata("OPF", "meta") or []
    for entry in metas:
        if isinstance(entry, tuple):
            _, attrs = entry
            if isinstance(attrs, dict) and attrs.get("name", "").lower() == "cover":
                cid = attrs.get("content")
                if cid:
                    img_it = book.get_item_with_id(cid)
                    if img_it and img_it.get_type() == ITEM_IMAGE:
                        return {"filename": img_it.get_name(), "bytes": img_it.get_content(), "mime": img_it.media_type}
    for img in book.get_items_of_type(ITEM_IMAGE):
        if "cover" in img.get_name().lower():
            return {"filename": img.get_name(), "bytes": img.get_content(), "mime": img.media_type}
    return None
