"""
Curie core: analysis pipeline orchestration.

Ported from Fank1/curie's worker.py (https://github.com/Fank1/curie) with
permission. Original author: Fank1. Adapted for MyBooks by shiningsprk-arch.

Replaces the Calibre QThread worker with a plain function + progress callback,
driven from a MyBooks toolbox background task.
"""

import json
import logging
import os
import re
import shutil

from .epub_injector import inject_footnotes
from .epub_utils import (build_name_pattern, get_spine_items, _to_text,
                         _to_llm_text)

logger = logging.getLogger("curie")

MIN_OCCURRENCES = 3


def extract_spine_paragraphs(epub_path):
    """Return [(chapter_num, para_num, text)] for every spine chapter's paragraphs.

    Uses the same deterministic paragraph split as _to_llm_text so paragraph
    numbers in the LLM chunk ([pN] markers) and at injection time always match.
    """
    from .epub_utils import get_spine_items

    items = []
    for chapter_num, _zip_path, html_bytes in get_spine_items(epub_path):
        for para_num, text in _to_llm_text(html_bytes):
            items.append((chapter_num, para_num, text))
    return items


def build_llm_chunks(paragraphs, chunk_token_budget=160_000):
    """Split (chapter, para, text) tuples into context-sized strings carrying
    ``=== CHAPTER N ===`` and ``[pN]`` markers for the enrich call."""
    chunks = []
    current = []
    current_tokens = 0
    for chapter_num, para_num, text in paragraphs:
        piece = f'\n=== CHAPTER {chapter_num} ===\n[p{para_num}] {text}\n'
        piece_tokens = len(piece.encode('utf-8')) // 3
        if current and current_tokens + piece_tokens > chunk_token_budget:
            chunks.append(''.join(current))
            current, current_tokens = [], 0
        current.append(piece)
        current_tokens += piece_tokens
    if current:
        chunks.append(''.join(current))
    return chunks


def _find_ambiguous_names(book_data):
    """Names that need LLM disambiguation at injection time:
      - a name used by >= 2 entities, or
      - a name that is a prefix of another entity's name (matching the short
        name would also match fragments of the longer name, e.g. 奥雷里亚诺
        inside 奥雷里亚诺·何塞).
    Returns a sorted set of ambiguous names (only those with actual matches).
    """
    name_owners = {}
    for key in ('characters', 'locations'):
        for ent in book_data.get(key, []):
            names = [ent.get('full_name') or ent.get('name') or '']
            names += [n for n in (ent.get('nicknames') or []) if n]
            names += [n for n in (ent.get('aliases') or []) if n]
            for n in names:
                if not n:
                    continue
                name_owners.setdefault(n, set()).add(id(ent))

    all_names = list(name_owners)
    ambiguous = set()
    for name in all_names:
        if len(name_owners[name]) >= 2:
            ambiguous.add(name)
            continue
        # the name appears INSIDE another entity's name (prefix, middle or
        # suffix, e.g. 乌尔苏拉 inside 阿玛兰妲·乌尔苏拉): matching it would
        # also hit fragments of the longer name.
        for other in all_names:
            if name != other and name in other:
                ambiguous.add(name)
                break
    return sorted(ambiguous)


def run_curie_analysis(provider, title, author, epub_path, include_characters,
                       include_places, language, output_path, progress_cb=None,
                       should_cancel=None):
    """
    Full analysis pipeline:
      Step 1: generate character/location JSON (web research or knowledge mode)
      Step 2a: programmatic occurrence counting
      Step 2b: LLM enrichment against the actual EPUB text (chunked)
      Step 2c: post-processing (schema fix, alias filter, chapter/occurrence, min-occurrences)
      Step 4: spoiler audit (reader-view review of every description)
      Save: sidecar JSON to output_path
    Returns the enriched book_data dict.
    """
    def _check_cancel():
        if should_cancel and should_cancel():
            raise RuntimeError("cancelled")

    _check_cancel()

    # ── Step 1 ──────────────────────────────────────────────────────────────
    if progress_cb:
        progress_cb('Step 1: Generating book data…')
    book_data, usage1, rate_headers = provider.generate_book_data(
        title=title, author=author,
        include_characters=include_characters, include_places=include_places,
        language=language, progress_cb=progress_cb,
    )
    _check_cancel()

    # ── Step 2a: programmatic counting ──────────────────────────────────────
    if progress_cb:
        progress_cb('Step 2 (EPUB spoiler analysis): Parsing EPUB…')
    # Parse the EPUB once; the same spine items feed both the plain-text
    # occurrence scan and the paragraph-preserving LLM chunks.
    spine_items = get_spine_items(epub_path)
    chapters = [(n, _to_text(html)) for n, _, html in spine_items]

    if progress_cb:
        progress_cb('Step 2 (EPUB spoiler analysis): Counting occurrences…')
    _add_chapter_and_occurrences(book_data, chapters)

    # Proactive rate-limit pause before Step 2 LLM calls (Anthropic only)
    epub_total_chars = sum(len(text) for _, text in chapters)
    tokens_needed = epub_total_chars // 4 + 5_000
    provider.wait_for_rate_limit(rate_headers, tokens_needed=tokens_needed,
                                 progress_cb=progress_cb)
    _check_cancel()

    # ── Step 2b: LLM enrichment (chunked for large books) ───────────────────
    # Paragraph-preserving chunks let the model resolve ambiguous names in a
    # single pass (see mentions below) — no second read of the book needed.
    paragraphs = [(n, pn, t) for n, _, html in spine_items
                  for pn, t in _to_llm_text(html)]
    epub_chunks = build_llm_chunks(paragraphs, provider.chunk_token_budget)
    n_chunks = len(epub_chunks)
    usage2 = {'input_tokens': 0, 'output_tokens': 0,
              'cache_write_tokens': 0, 'cache_read_tokens': 0}
    current_data = book_data
    ambiguous_names = _find_ambiguous_names(book_data)
    mentions = []

    for i, chunk_text in enumerate(epub_chunks):
        chunk_label = f'part {i + 1}/{n_chunks}' if n_chunks > 1 else ''
        candidate, chunk_usage, chunk_mentions = provider.enrich_book_data(
            current_data, chunk_text, ambiguous_names=ambiguous_names,
            progress_cb=progress_cb, chunk_label=chunk_label,
        )
        for k in usage2:
            usage2[k] += chunk_usage.get(k, 0)
        if chunk_mentions:
            mentions.extend(chunk_mentions)
        candidate = _unwrap_entities(candidate)
        if isinstance(candidate, dict) and ('characters' in candidate or 'locations' in candidate):
            current_data = candidate
        else:
            # malformed response — keep the previous round's data rather than
            # wiping out all entities
            logger.warning("[curie] enrich chunk %d returned malformed structure, keeping previous data", i + 1)
        # the model sometimes drops the top-level title/author — restore them
        # so later chunks and the review pass keep the book identity
        current_data.setdefault('title', title)
        current_data.setdefault('author', author)
        _check_cancel()

    book_data_enriched = current_data

    # ── Step 2c: post-processing ────────────────────────────────────────────
    _fix_schema(book_data_enriched)
    _dedupe_entities(book_data_enriched)
    _filter_aliases(book_data_enriched)
    # chapter/occurrences are ALWAYS recomputed programmatically here — never
    # trust the LLM with them (a wrong "first chapter" makes hints appear
    # before the reader knows the character, i.e. spoilers).
    _add_chapter_and_occurrences(book_data_enriched, chapters, only_missing=False)
    _add_active_ranges(book_data_enriched, chapters)
    _filter_by_occurrences(book_data_enriched)

    # ── Step 4: spoiler audit (reader-view) ─────────────────────────────────
    review_for_spoilers(provider, book_data_enriched, chapters, progress_cb)
    _check_cancel()

    # mentions: [(name, chapter, para) -> entity index] for ambiguous names
    book_data_enriched['mentions'] = _normalize_mentions(mentions, book_data_enriched)

    # ── Save sidecar JSON ───────────────────────────────────────────────────
    if progress_cb:
        progress_cb('Saving output…')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(book_data_enriched, f, indent=2, ensure_ascii=False)

    return book_data_enriched


SPOILER_REVIEW_BUDGET = 40_000      # tokens per spoiler-audit chunk
SPOILER_WINDOW_CHARS = 150          # chars around each name occurrence
SPOILER_MAX_WINDOWS = 12            # per entity


def _build_spoiler_material(entity, chapters):
    """Collect text windows around the entity's name occurrences in chapters
    AFTER its first chapter — this is the spoiler source used by the audit.

    Windows are spread evenly across the late chapters (each chapter gets
    roughly SPOILER_MAX_WINDOWS / n_late windows) so that story-end spoilers
    are not starved out by a front-loaded quota.
    """
    names = [entity.get('full_name') or entity.get('name') or '']
    names += [n for n in (entity.get('nicknames') or []) if n]
    names = [n for n in names if n]
    pats = [build_name_pattern(n) for n in names]
    first_ch = int(entity.get('chapter') or 0)
    late = [(n, t) for n, t in chapters if n > first_ch]
    n_late = len(late)
    if not n_late:
        return ''
    if n_late <= SPOILER_MAX_WINDOWS:
        base = SPOILER_MAX_WINDOWS // n_late
        extra = SPOILER_MAX_WINDOWS % n_late
        start = 0
    else:
        # more late chapters than windows: one window per chapter, taken
        # from the END of the book (story-end spoilers are the priority)
        base, extra, start = 1, 0, n_late - SPOILER_MAX_WINDOWS
    windows = []
    for i, (ch_num, text) in enumerate(late[start:], start=start):
        cap = base + (1 if i < extra else 0)
        got = 0
        for pat in pats:
            for m in pat.finditer(text):
                if got >= cap:
                    break
                s = max(0, m.start() - SPOILER_WINDOW_CHARS)
                e = min(len(text), m.end() + SPOILER_WINDOW_CHARS)
                windows.append(f'[ch{ch_num}] …{text[s:e]}…')
                got += 1
            if got >= cap:
                break
        if len(windows) >= SPOILER_MAX_WINDOWS:
            break
    return '\n'.join(windows)


def _normalize_timeline(entity):
    """Sort/dedupe the entity's progressive-hint timeline, coerce up_to to int.

    Returns a list of {"up_to": int, "description": str} or None if invalid.
    The last stage is always made to match entity["description"] (the final
    version shown in previews and fallback injections).
    """
    raw = entity.get('timeline')
    if not isinstance(raw, list) or not raw:
        return None
    stages = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            up_to = int(item.get('up_to'))
        except (TypeError, ValueError):
            continue
        desc = (item.get('description') or '').strip()
        if not desc or up_to in seen:
            continue
        seen.add(up_to)
        stages.append({'up_to': up_to, 'description': desc})
    if not stages:
        return None
    stages.sort(key=lambda s: s['up_to'])
    # last stage must cover the whole book == final description
    stages[-1]['description'] = entity.get('description') or stages[-1]['description']
    stages[-1]['up_to'] = max(s['up_to'] for s in stages)
    return stages


def review_for_spoilers(provider, book_data, chapters, progress_cb=None):
    """Step 4: reader-view spoiler audit + progressive timeline construction.

    For every entity, the model receives only LATER-chapter text windows
    (where its name occurs after its first chapter) as the spoiler source,
    then:
      - rewrites any description that reveals post-introduction knowledge;
      - builds a "timeline" of 2-5 stages (model decides the count) so that
        footnote hints can evolve with the story, each stage spoiler-free
        for a reader who has reached that chapter.
    Results are applied to book_data in place.
    """
    entities = []
    for key in ('characters', 'locations'):
        entities += [(key, e) for e in book_data.get(key, [])
                     if e.get('full_name') or e.get('name')]
    if not entities:
        return book_data

    if progress_cb:
        progress_cb('Step 4 (spoiler audit): building materials…')

    groups = []
    current = []
    current_tokens = 0
    for key, ent in entities:
        material = _build_spoiler_material(ent, chapters)
        if not material:
            continue
        ent_tokens = len(material.encode('utf-8')) // 3
        if current and current_tokens + ent_tokens > SPOILER_REVIEW_BUDGET:
            groups.append(current)
            current, current_tokens = [], 0
        current.append((key, ent, material))
        current_tokens += ent_tokens
    if current:
        groups.append(current)

    fixes = {}  # (key, name) -> corrected item
    n_groups = len(groups)
    for gi, group in enumerate(groups):
        material = '\n\n'.join(mat for _, _, mat in group)
        entries = {'characters': [], 'locations': []}
        for key, ent, _mat in group:
            entries[key].append({
                'full_name': ent.get('full_name') or ent.get('name', ''),
                'name': ent.get('name') or ent.get('full_name', ''),
                'nicknames': ent.get('nicknames', []),
                'role': ent.get('role', ''),
                'type': ent.get('type', ''),
                'chapter': ent.get('chapter', 0),
                'description': ent.get('description', ''),
            })
        label = f'part {gi + 1}/{n_groups}' if n_groups > 1 else ''
        if progress_cb:
            progress_cb(f'Step 4 (spoiler audit): auditing{(" " + label) if label else ""}…')
        result = provider.review_spoilers(
            book_data, material, entries, progress_cb=progress_cb, chunk_label=label,
        )
        for key in ('characters', 'locations'):
            for item in result.get(key, []):
                name = item.get('full_name') or item.get('name')
                if name:
                    fixes[(key, name)] = item

    for key in ('characters', 'locations'):
        for ent in book_data.get(key, []):
            name = ent.get('full_name') or ent.get('name')
            item = fixes.get((key, name))
            if not item:
                continue
            desc = (item.get('description') or '').strip()
            if desc and desc != ent.get('description'):
                ent['description'] = desc
            if item.get('timeline') is not None:
                ent['timeline'] = _normalize_timeline(item)
    return book_data


def build_curied_epub(src_epub_path, dst_epub_path, book_data,
                      hint_density='every_10_paragraphs', progress_cb=None):
    """Copy the source EPUB to dst and inject footnote hints into the copy.

    The original library file is never touched; the injected copy is what gets
    re-imported as a new book.
    """
    shutil.copy2(src_epub_path, dst_epub_path)
    if progress_cb:
        progress_cb('Step 3: Injecting footnote hints into EPUB…')
    stats = inject_footnotes(dst_epub_path, book_data, 'koreader', hint_density)
    return stats or {}


def load_book_data(path):
    """Load a saved sidecar JSON, or None if missing/corrupt."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def total_cost(provider, usage1, usage2):
    """USD cost for display: actual measured usage."""
    return provider.calc_cost(usage1) + provider.calc_cost(usage2)


# ── Post-processing helpers (ported from curie worker.py) ──────────────────────

def _scan(names, chapters):
    first_chapter = None
    total = 0
    for chapter_num, text in chapters:
        count = 0
        for n in names:
            if not n:
                continue
            count += len(build_name_pattern(n).findall(text))
        total += count
        if count > 0 and first_chapter is None:
            first_chapter = chapter_num
    return first_chapter, total


def _add_chapter_and_occurrences(book_data, chapters, only_missing=False):
    for char in book_data.get('characters', []):
        if only_missing and char.get('chapter') is not None:
            continue
        primary = char.get('full_name') or char.get('name', '')
        if not primary:
            continue
        names = [primary] + [n for n in (char.get('nicknames') or []) if n]
        char['chapter'], char['occurrences'] = _scan(names, chapters)

    for loc in book_data.get('locations', []):
        if only_missing and loc.get('chapter') is not None:
            continue
        primary = loc.get('name') or loc.get('full_name', '')
        if not primary:
            continue
        aliases = [a for a in (loc.get('aliases') or []) if a]
        loc['chapter'], loc['occurrences'] = _scan([primary] + aliases, chapters)


def _fix_schema(book_data):
    """Drop entries where the LLM returned the wrong field names; remove spurious top-level fields."""
    book_data.pop('note', None)
    book_data['characters'] = [
        c for c in book_data.get('characters', [])
        if c.get('full_name') or c.get('name')
    ]
    book_data['locations'] = [
        loc for loc in book_data.get('locations', [])
        if loc.get('name') or loc.get('full_name')
    ]


def _norm_name(name):
    """Normalize a name for duplicate detection: case/space-insensitive."""
    return (name or '').strip().lower().replace(' ', '')


def _unwrap_entities(data):
    """Unwrap stray nesting like {'entities': {'entities': {...}}} from LLM
    responses; returns the innermost dict that actually holds characters."""
    while isinstance(data, dict) and isinstance(data.get('entities'), dict):
        inner = data['entities']
        if 'characters' in inner or 'locations' in inner:
            data = inner
        else:
            break
    return data


def _entity_index_by_label(book_data, label):
    """Map an '#N' (1-based) or bare int label to a 0-based entity index."""
    try:
        idx = int(str(label).lstrip('#')) - 1
    except (TypeError, ValueError):
        return None
    total = len(book_data.get('characters', [])) + len(book_data.get('locations', []))
    return idx if 0 <= idx < total else None


def _entities_matching_name(book_data, name):
    """Entity indices whose names (full_name / name / nicknames / aliases)
    include the given name. Names shared by several entities return several
    matches — that is exactly the ambiguity the mentions table resolves."""
    name = (name or '').strip()
    if not name:
        return []
    matches = []
    for key in ('characters', 'locations'):
        for idx, ent in enumerate(book_data.get(key, [])):
            names = {ent.get('full_name') or '', ent.get('name') or ''}
            names |= set(ent.get('nicknames') or [])
            names |= set(ent.get('aliases') or [])
            if name in names:
                matches.append(idx)
    return matches


def _normalize_mentions(mentions, book_data):
    """Validate/dedupe mention assignments from the enrich pass.

    Each mention: {"name": str, "chapter": int, "para": int, "entity": "#N"}
    Output: {"name", "chapter", "para", "entity": int(0-based)}.
    Invalid entries are dropped (injection falls back to active-range order).

    The entity reference is resolved in two steps:
      1. by NAME — when the mention's name matches exactly ONE entity in the
         final (deduplicated + filtered) list, that entity wins. This fixes
         index drift introduced by _dedupe_entities (merges) and
         _filter_by_occurrences (deletions), which shift the positional
         numbering the LLM saw during enrichment;
      2. by '#N' label — used when the name is shared by several entities
         (the genuine ambiguity case, where positional order is stable
         because neither dedupe nor filter touched the involved entities).
    """
    out = []
    seen = set()
    for m in mentions or []:
        if not isinstance(m, dict):
            continue
        name = (m.get('name') or '').strip()
        try:
            chapter = int(m.get('chapter'))
            para = int(m.get('para'))
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        by_name = _entities_matching_name(book_data, name)
        if len(by_name) == 1:
            idx = by_name[0]
        else:
            idx = _entity_index_by_label(book_data, m.get('entity'))
        if idx is None:
            continue
        key = (name, chapter, para)
        if key in seen:
            continue
        seen.add(key)
        out.append({'name': name, 'chapter': chapter, 'para': para, 'entity': idx})
    return out


def _dedupe_entities(book_data):
    """Merge exact-duplicate entities (the LLM sometimes emits the same person
    twice — e.g. two "何塞·阿尔卡蒂奥" entries). Nicknames are unioned, the
    longer description and the richest timeline win; occurrences/chapter are
    recomputed later anyway."""
    for key in ('characters', 'locations'):
        seen = {}
        result = []
        for ent in book_data.get(key, []):
            name = ent.get('full_name') or ent.get('name') or ''
            norm = _norm_name(name)
            if not norm:
                continue
            if norm in seen:
                base = seen[norm]
                nicks = set((base.get('nicknames') or []) + (ent.get('nicknames') or []))
                base['nicknames'] = sorted(nicks, key=len, reverse=True)
                if len(ent.get('description') or '') > len(base.get('description') or ''):
                    base['description'] = ent['description']
                if len(ent.get('timeline') or []) > len(base.get('timeline') or []):
                    base['timeline'] = ent['timeline']
            else:
                seen[norm] = ent
                result.append(ent)
        book_data[key] = result


def _add_active_ranges(book_data, chapters):
    """Compute active_from/active_to for every entity from where its names
    (full name + nicknames) actually occur in the text.

    Used by the injector to resolve short-name ambiguity: when two entities
    share a short name (e.g. 阿玛兰妲 for both the 2nd- and 5th-generation
    characters), the entity whose active range covers the current chapter
    gets injected first and therefore wins the shared name in that chapter.
    """
    for key in ('characters', 'locations'):
        for ent in book_data.get(key, []):
            names = [ent.get('full_name') or ent.get('name') or '']
            names += [n for n in (ent.get('nicknames') or []) if n]
            pats = [build_name_pattern(n) for n in names if n]
            if not pats:
                continue
            first = None
            last = None
            for ch_num, text in chapters:
                if any(p.search(text) for p in pats):
                    if first is None:
                        first = ch_num
                    last = ch_num
            if first is not None:
                ent['active_from'] = first
                ent['active_to'] = last


_CJK_CHAR_RE = re.compile(r'[\u3400-\u9fff]')


def _is_valid_alias(alias):
    """Keep aliases that are proper names: Latin aliases must start with an
    uppercase letter; CJK aliases (Chinese/Japanese) are always accepted since
    they have no case."""
    if not alias:
        return False
    if _CJK_CHAR_RE.search(alias):
        return True
    return alias[0].isupper()


def _filter_aliases(book_data):
    for char in book_data.get('characters', []):
        char['nicknames'] = [n for n in (char.get('nicknames') or []) if _is_valid_alias(n)]
    for loc in book_data.get('locations', []):
        primary_words = set((loc.get('name') or loc.get('full_name', '')).lower().split())
        kept = []
        for a in (loc.get('aliases') or []):
            if not _is_valid_alias(a):
                continue
            is_cjk = bool(_CJK_CHAR_RE.search(a))
            if is_cjk:
                if len(a) < 2:
                    continue  # single CJK char too broad
            elif len(a.split()) < 2:
                continue  # single Latin word too broad
            if set(a.lower().split()).issubset(primary_words):
                continue  # must add at least one new word
            kept.append(a)
        loc['aliases'] = kept


def _filter_by_occurrences(book_data, min_occurrences=MIN_OCCURRENCES):
    for key in ('characters', 'locations'):
        book_data[key] = [
            e for e in book_data.get(key, [])
            if (e.get('occurrences') or 0) >= min_occurrences
        ]
