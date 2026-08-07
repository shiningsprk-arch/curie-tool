"""
Curie core: EPUB parsing utilities.

Ported from Fank1/curie (https://github.com/Fank1/curie) with permission.
Original author: Erik Fanki. Adapted for MyBooks by shiningsprk-arch.
"""

import re
import zipfile

from lxml import etree

_NS_CONTAINER = 'urn:oasis:names:tc:opendocument:xmlns:container'
_NS_OPF = 'http://www.idpf.org/2007/opf'
_NS_XHTML = 'http://www.w3.org/1999/xhtml'

# epub:type values that identify non-chapter front/back matter.
_FRONT_MATTER_RE = re.compile(
    r'epub:type\s*=\s*["\'][^"\']*\b('
    r'cover|title-page|titlepage|toc|landmarks|frontmatter|'
    r'halftitlepage|copyright-page|dedication|colophon|'
    r'index|loi|lot|seriespage'
    r')\b',
    re.IGNORECASE,
)

# Matches any space-like separator as it may appear in raw HTML source.
_SPACE_PAT = '(?:[ ?]|&#160;|&nbsp;)'

# CJK ideographs (incl. Extension A), used for name-boundary decisions.
_CJK_RE = re.compile(r'[\u3400-\u9fff]')


def build_name_pattern(name):
    """Compile a CJK-friendly word-boundary regex for a character/location name.

    Upstream curie uses plain ``\\b...\\b`` which fails for CJK text
    (e.g. ``张三`` never matches inside ``张三走进房间`` because CJK chars
    count as word characters). This helper instead:
      - blocks a Latin/digit prefix so ``Alice`` doesn't match ``MyAlice``;
      - blocks a CJK prefix only for single-character CJK names, so ``三``
        doesn't match inside ``刘三``, while multi-char names like ``张三``
        still match in the ubiquitous patterns ``说张三`` / ``张三说``;
      - allows CJK characters immediately after the name (``张三说``).
    """
    raw_words = [w for w in (name or '').split() if w]
    if not raw_words:
        return re.compile(r'(?!)')
    first_raw = raw_words[0]
    words = [re.escape(w) for w in raw_words]
    body = _SPACE_PAT.join(words)
    if _CJK_RE.search(first_raw) and len(first_raw) == 1:
        start = r'(?<![\u3400-\u9fff])'
    else:
        start = r'(?<![A-Za-z0-9_])'
    return re.compile(start + body + r'(?![A-Za-z0-9_])', re.IGNORECASE)


def get_spine_items(epub_path):
    """Return list of (chapter_number, zip_path, html_bytes) for each content spine item."""
    items = []
    chapter_num = 0

    with zipfile.ZipFile(epub_path, 'r') as zf:
        opf_path = _find_opf_path(zf)
        opf_dir = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''
        opf = etree.fromstring(zf.read(opf_path))

        manifest = {
            item.get('id'): item.get('href')
            for item in opf.findall(f'.//{{{_NS_OPF}}}item')
        }
        spine = opf.find(f'.//{{{_NS_OPF}}}spine')

        for itemref in spine.findall(f'{{{_NS_OPF}}}itemref'):
            # Skip items explicitly marked as non-linear (covers, TOC pages, etc.)
            if itemref.get('linear', 'yes').lower() == 'no':
                continue

            sid = itemref.get('idref', '')
            href = manifest.get(sid, '')
            if not href:
                continue
            full_path = _resolve(opf_dir, href)
            try:
                content = zf.read(full_path)
            except KeyError:
                continue

            # Skip front/back matter identified by epub:type on body or section
            raw = content[:2000].decode('utf-8', errors='replace')
            if _FRONT_MATTER_RE.search(raw):
                continue

            text = _to_text(content)
            if len(text.strip()) < 100:
                continue
            chapter_num += 1
            items.append((chapter_num, full_path, content))

    return items


def extract_spine_texts(epub_path):
    """Return list of (chapter_number, plain_text) for each content spine item."""
    return [(n, _to_text(html)) for n, _, html in get_spine_items(epub_path)]


# Token budget per chunk: 160k tokens for epub text, leaving ~40k for user message
# and system prompt overhead. Uses bytes//3 as the token estimate so the limit
# is accurate for both ASCII (1 byte/char) and Japanese/CJK (3 bytes/char).
CHUNK_TOKEN_BUDGET = 160_000


def build_epub_chunks(chapters, chunk_token_budget=CHUNK_TOKEN_BUDGET):
    """Split chapters into context-window-sized strings for LLM enrichment."""
    chunks, current, current_tokens = [], [], 0
    for chapter_num, text in chapters:
        piece = f'\n=== CHAPTER {chapter_num} ===\n{text}\n'
        piece_tokens = len(piece.encode('utf-8')) // 3
        if current and current_tokens + piece_tokens > chunk_token_budget:
            chunks.append(''.join(current))
            current, current_tokens = [], 0
        current.append(piece)
        current_tokens += piece_tokens
    if current:
        chunks.append(''.join(current))
    return chunks


# ── Internal helpers ────────────────────────────────────────────────────────────

def _find_opf_path(zf):
    container = etree.fromstring(zf.read('META-INF/container.xml'))
    return container.find(f'.//{{{_NS_CONTAINER}}}rootfile').get('full-path')


def _resolve(base_dir, href):
    href = href.split('#')[0].split('?')[0]
    return href.lstrip('/') if href.startswith('/') else base_dir + href


def _to_text(html_bytes):
    try:
        root = etree.fromstring(html_bytes)
    except etree.XMLSyntaxError:
        try:
            from lxml import html as lhtml
            root = lhtml.fromstring(html_bytes)
        except Exception:
            return ''

    for tag in root.iter(
        f'{{{_NS_XHTML}}}script', f'{{{_NS_XHTML}}}style', 'script', 'style'
    ):
        parent = tag.getparent()
        if parent is not None:
            parent.remove(tag)

    return ' '.join(' '.join(root.itertext()).split())


# Block-level tags that delimit paragraphs for the LLM-facing text.
# <br> is intentionally NOT a paragraph delimiter (it merges into the
# surrounding paragraph), so the numbering stays in sync with the injector.
_BLOCK_TAGS = {
    'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'tr',
}

# Same tag set, as a regex over raw opening tags. Shared with the injector so
# paragraph numbers counted at injection time match the [pN] markers the LLM
# saw — empty paragraphs occupy a slot in BOTH sides.
_BLOCK_OPEN_RE = re.compile(r'<(?:p|div|li|h[1-6]|blockquote|tr)[\s/>]', re.IGNORECASE)


def _to_llm_text(html_bytes):
    """Extract paragraph-preserving plain text for LLM chunks.

    Unlike _to_text (which flattens everything into one line), this keeps
    block-level boundaries so the chunk can carry ``[pN]`` markers and the
    model can point at exact paragraphs when resolving ambiguous names.

    Paragraph numbering is deterministic and slot-based: every block-level
    opening tag occupies one number, INCLUDING empty paragraphs, so the
    numbering matches the injector's paragraph counter exactly.

    Returns a list of (para_number, text).
    """
    try:
        root = etree.fromstring(html_bytes)
    except etree.XMLSyntaxError:
        try:
            from lxml import html as lhtml
            root = lhtml.fromstring(html_bytes)
        except Exception:
            return []

    for tag in root.iter(
        f'{{{_NS_XHTML}}}script', f'{{{_NS_XHTML}}}style', 'script', 'style'
    ):
        parent = tag.getparent()
        if parent is not None:
            parent.remove(tag)

    paragraphs = []
    buffer = []
    counter = [0]
    # True once any block tag has been seen anywhere in this document.
    # Before that point non-block text is leading text (occupies no slot);
    # after it, all text belongs to the current paragraph slot.
    seen_block = [False]

    def _flush():
        text = ' '.join(' '.join(buffer).split())
        counter[0] += 1
        paragraphs.append((counter[0], text))
        buffer.clear()

    def walk(node):
        for child in node.iterchildren():
            if not isinstance(child.tag, str):
                continue
            tag = etree.QName(child).localname.lower()
            if tag in _BLOCK_TAGS:
                # No leading empty slot: the first block tag opens slot #1.
                if seen_block[0] and (buffer or paragraphs):
                    _flush()
                seen_block[0] = True
                if child.text:
                    buffer.append(child.text)
                walk(child)
                if child.tail:
                    buffer.append(child.tail)
            elif seen_block[0]:
                if child.text:
                    buffer.append(child.text)
                walk(child)
                if child.tail:
                    buffer.append(child.tail)
            else:
                # Leading non-block content (e.g. <span>/<header> before the
                # first block tag): its text is dropped so slot #1 is the
                # first block tag — exactly what the injector's _BLOCK_OPEN_RE
                # counter sees. Children are still walked: nested block tags
                # must occupy their slots or paragraph numbers drift.
                walk(child)
                # child.tail is leading text as well -> dropped

    body = root.find(f'{{{_NS_XHTML}}}body')
    if body is None:
        body = root

    walk(body)
    _flush()
    return paragraphs
