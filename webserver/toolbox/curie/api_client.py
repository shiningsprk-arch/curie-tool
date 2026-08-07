"""
Curie core: LLM API clients.

Dual-mode provider abstraction:
- AnthropicProvider: faithful port of Fank1/curie's api_client.py (Claude +
  built-in web_search tool + prompt caching + cost estimation + rate limits).
- OpenAICompatProvider: OpenAI-compatible chat/completions endpoints
  (DeepSeek / Qwen / SiliconFlow etc.) without built-in web search
  (knowledge-only Step 1, slightly lower quality).

Original curie api_client by Erik Fanki (https://github.com/Fank1/curie),
used with permission. Adapted for MyBooks by shiningsprk-arch.
"""

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    import requests as _requests

    def _http_post(url, headers, payload, timeout=180):
        r = _requests.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json(), {k.lower(): v for k, v in r.headers.items()}

except ImportError:  # pragma: no cover - fallback without requests
    import urllib.request
    import urllib.error

    def _http_post(url, headers, payload, timeout=180):
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return json.loads(resp.read().decode('utf-8')), resp_headers
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode('utf-8', errors='replace')
            except Exception:
                body = ''
            raise Exception(f'HTTP {e.code}: {e.reason} — {body}') from e


ANTHROPIC_DEFAULT_URL = 'https://api.anthropic.com'
OPENAI_COMPAT_DEFAULT_URL = 'https://api.deepseek.com'
API_VERSION = '2023-06-01'

# Anthropic pricing per million tokens (USD), for cost estimation display.
ANTHROPIC_MODELS = {
    'claude-sonnet-4-6': {
        'label': 'Claude Sonnet 4.6 — Better quality',
        'input': 3.00,
        'output': 15.00,
        'cache_write': 3.75,
        'cache_read': 0.30,
        'estimate_step1': (0.20, 0.35),
        'estimate_step2': (0.08, 0.15),
    },
    'claude-haiku-4-5-20251001': {
        'label': 'Claude Haiku 4.5 — Faster & cheaper',
        'input': 0.80,
        'output': 4.00,
        'cache_write': 1.00,
        'cache_read': 0.08,
        'estimate_step1': (0.05, 0.10),
        'estimate_step2': (0.02, 0.05),
    },
}

# Rough per-million-token pricing (USD) for OpenAI-compatible models.
# Fallback values are close to DeepSeek-chat pricing.
OPENAI_COMPAT_MODEL_PRICING = {
    'deepseek-chat': {'input': 0.27, 'output': 1.10},
    'deepseek-reasoner': {'input': 0.55, 'output': 2.19},
}

ANTHROPIC_CHUNK_TOKEN_BUDGET = 160_000
OPENAI_COMPAT_CHUNK_TOKEN_BUDGET = 40_000


# ── Shared helpers ──────────────────────────────────────────────────────────────

def extract_json(text):
    """Parse the JSON object from an LLM response, tolerating stray text.

    Handles code fences, surrounding prose and even multiple JSON objects
    (some models append extra output) by picking the LAST complete object.
    """
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*\n?', '', text)
    text = re.sub(r'\n?```\s*$', '', text)
    text = text.strip()
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    results = []
    idx = 0
    while idx < len(text):
        try:
            obj, idx = decoder.raw_decode(text, idx)
            results.append(obj)
        except json.JSONDecodeError:
            nxt = text.find('{', idx + 1)
            if nxt == -1:
                break
            idx = nxt
            continue
        while idx < len(text) and text[idx] in ' \t\r\n':
            idx += 1
    if results:
        return results[-1]
    raise ValueError('extract_json: no valid JSON object found in response')


def _countdown_sleep(seconds, label, progress_cb=None):
    """Sleep with a live per-second countdown emitted via progress_cb."""
    for remaining in range(int(seconds), 0, -1):
        if progress_cb:
            progress_cb(f'{label} — {remaining}s remaining…')
        time.sleep(1)


def _wait_429(exception, attempt, progress_cb=None, max_retries=3):
    """Sleep on 429 rate limits, return True if retry is allowed."""
    if '429' in str(exception) and attempt < max_retries:
        wait = 60 * (attempt + 1)
        _countdown_sleep(wait, 'Rate limit', progress_cb)
        return True
    return False


def build_step1_prompt(title, author, include_characters, include_places, language,
                       with_search: bool):
    """Build the Step 1 prompt shared by both providers."""
    field_specs = []
    if include_characters:
        field_specs.append(
            f'characters: array of objects with:\n'
            f'  - full_name (string)\n'
            f'  - nicknames (array of strings): MUST include the exact short forms '
            f'actually used in the book text — e.g. for a Chinese book add the Chinese '
            f'short name like 阿玛兰妲, never only a Latin transliteration like '
            f'"Amaranta" that never appears in the text. Latin nicknames start with a '
            f'capital letter; Chinese/Japanese nicknames are kept as-is.\n'
            f'  - role: protagonist | major character | supporting character | minor character\n'
            f'  - description: 2-3 sentences, SPOILER-FREE, written in {language}'
        )
    if include_places:
        field_specs.append(
            f'locations: array of objects with:\n'
            f'  - name (string)\n'
            f'  - type (string, e.g. shop / landmark / residential / street / workplace)\n'
            f'  - aliases (array of strings; same rule as nicknames: include the exact '
            f'forms used in the text, Latin ones start with a capital letter)\n'
            f'  - description: 2-3 sentences, SPOILER-FREE, written in {language}'
        )

    what = ' and '.join(
        (['characters'] if include_characters else [])
        + (['locations'] if include_places else [])
    )
    skeleton = {'title': title, 'author': author}
    if include_characters:
        skeleton['characters'] = []
    if include_places:
        skeleton['locations'] = []

    research_note = (
        'Use the web search tool to research the book and gather accurate facts.\n'
    ) if with_search else (
        'Use your own knowledge of the book to gather accurate facts. '
        'You do NOT have a web search tool, so do not claim to search; just answer from knowledge.\n'
    )

    prompt = (
        f'Research the book "{title}" by {author} and generate a JSON with {what}.\n\n'
        + research_note
        + f'Return ONLY a raw JSON object (no markdown, no code fences) matching:\n'
        f'{json.dumps(skeleton, indent=2)}\n\n'
        f'Required fields:\n' + '\n'.join(field_specs) + '\n\n'
        f'Rules:\n'
        f'- All description content MUST be in {language}\n'
        f'- All JSON keys MUST be in English\n'
        f'- Descriptions must be SPOILER-FREE\n'
        f'- Cover all significant {what}\n'
        f'- Nicknames and aliases must be proper names in natural title case '
        f'(e.g. "Mr. Nakano") for Latin scripts; Chinese/Japanese names are '
        f'kept as-is\n'
        f'- Return ONLY the raw JSON'
    )
    return prompt


def _split_enrich_result(parsed):
    """Extract the entities dict from an enrich response.

    New format: {"entities": {...}, "mentions": [...]}. Old format (a bare
    entities object) is accepted for backward compatibility. Also unwraps
    double-nested {"entities": {"entities": ...}} and tolerates a bare
    characters array under "entities" (some models emit one).
    """
    if isinstance(parsed, dict):
        inner = parsed.get('entities')
        while isinstance(inner, dict) and isinstance(inner.get('entities'), dict):
            inner = inner['entities']
        if isinstance(inner, dict) and ('characters' in inner or 'locations' in inner):
            return inner
        if isinstance(inner, list) and 'characters' not in parsed:
            # model emitted a bare array for "entities" — wrap it
            return {'characters': inner, 'locations': parsed.get('locations', [])}
    return parsed


def build_enrich_prompt(book_data, epub_text, language, ambiguous_names=None):
    """Build the Step 2 (EPUB enrichment) user prompt.

    Two outputs in one pass (the model reads the book text exactly once):
      - "entities": the enriched character/location JSON (spelling fixes,
        extra nicknames, spoiler removal — same as upstream curie);
      - "mentions": paragraph-level assignments resolving AMBIGUOUS names
        (names shared by several entities, or prefixes of longer names).
    Entities are numbered #1..#N by their order in the JSON, which anchors
    identity across chunks and across the later spoiler-audit pass.
    """
    ambiguous_note = ''
    if ambiguous_names:
        ambiguous_note = (
            'AMBIGUOUS NAMES — paragraph-level disambiguation required.\n'
            'These names are shared by several entities or are prefixes of '
            'longer names, so a bare string match cannot tell who they refer '
            'to. In the book text below (marked [pN] per paragraph), find '
            'every paragraph where an ambiguous name occurs and decide, from '
            'the surrounding context, which entity it refers to.\n'
            f'Ambiguous names: {ambiguous_names}\n'
            'Output a "mentions" array with one entry per (name, chapter, '
            'paragraph) where you can determine the referent:\n'
            '  {"name": "<the ambiguous name>", "chapter": <chapter number>, '
            '"para": <[pN] number>, "entity": "#<N>"}\n'
            'Use the entity numbers from the entities JSON (first entity is '
            '#1, second is #2, ...). If a paragraph contains the name more '
            'than once, one entry covers the whole paragraph. Omit paragraphs '
            'you cannot determine with confidence — the system falls back to '
            'heuristics there.\n\n'
        )
    return (
        f'Below is the character/location JSON for "{book_data.get("title", "")}" '
        f'by {book_data.get("author", "")}, generated from research. Entities are '
        f'numbered in order: the first character is #1, etc.\n\n'
        f'{json.dumps(book_data, indent=2, ensure_ascii=False)}\n\n'
        'Using the book text below, produce a JSON object with two keys:\n\n'
        '1. "entities": the complete enriched character/location JSON '
        '(same top-level structure and ALL fields), after:\n'
        '   a. Correcting misspelled names to match actual spelling in the book\n'
        '   b. Adding missing nicknames/aliases (proper names in natural title '
        'case only for Latin scripts, never all-caps; Chinese/Japanese names '
        'kept as-is; include short forms actually used in the text)\n'
        '   c. Removing spoilers from descriptions — each entity has a '
        '"chapter" field indicating where it first appears; descriptions must '
        'only reflect what a reader would know up to and including that '
        'chapter, never revealing events or details from later chapters\n'
        '   d. Adding significant missing characters/locations, including the '
        'protagonist or first-person narrator if absent\n'
        '   e. NOT adding or modifying "chapter" or "occurrences" fields\n\n'
        + ambiguous_note
        + '2. "mentions": as described above.\n\n'
        'Output shape (STRICTLY):\n'
        '{"entities": {"title": "...", "author": "...", '
        '"characters": [ {full_name, nicknames, role, description, chapter, '
        'occurrences}, ... ], "locations": [ ... ]}, '
        '"mentions": [ {"name": "...", "chapter": N, "para": N, '
        '"entity": "#N"}, ... ]}\n'
        '"entities" must be a JSON OBJECT holding the whole book structure — '
        'never a bare array.\n'
        'Return ONLY that JSON object. No markdown, no explanation.'
    )


def build_review_prompt(book_data, entries):
    """Build the Step 4 prompt shared by both providers.

    Two jobs in one pass:
      1. spoiler audit — rewrite any description that reveals post-introduction
         knowledge (reader-view: the entity's "chapter" field);
      2. timeline construction — split each description into progressive stages
         (model decides how many), each safe for a reader who has read up to
         that chapter, so footnotes can evolve with the story.
    """
    return (
        f'Book: "{book_data.get("title", "")}" by {book_data.get("author", "")}.\n\n'
        'Below are character/location descriptions to audit and expand:\n'
        f'{json.dumps(entries, indent=2, ensure_ascii=False)}\n\n'
        'PART 1 — SPOILER AUDIT\n'
        'A spoiler is anything a reader could not know by the end of the chapter '
        'where the entity first appears (its "chapter" field): later events, deaths, '
        'marriages, betrayals, identity reveals, outcomes, comparative claims about '
        'the future (e.g. "the longest-lived"), late-story traits. The LATER-book '
        'material in your system prompt is the spoiler source — use it to check.\n'
        'Rewrite any spoiling description: accurate, but safe for a reader who has '
        'just met the character. Written in the SAME language as the original.\n\n'
        'PART 2 — TIMELINE (progressive hints)\n'
        'For EVERY entity, build a "timeline" array of 2-5 stages that lets footnote '
        'hints grow with the story. Split at major plot beats (introduction, development, '
        'turning points, resolution). Each stage is:\n'
        '  {"up_to": <chapter number>, "description": "<what the reader knows up to '
        'that chapter — strictly spoiler-free for a reader who has reached it>"}\n'
        'Rules: up_to must be an integer >= the entity\'s "chapter"; stages sorted '
        'ascending; the LAST stage covers the whole book and equals the fully safe, '
        'complete description; every stage in the SAME language as the original.\n\n'
        'Return ONLY a JSON object like {"characters": [...], "locations": [...]} '
        'containing EVERY entity from the input, each with "full_name"/"name", the '
        'corrected "description" (final version), and the "timeline" array. '
        'Omit nothing — all entities must come back.'
    )


def make_provider(provider, api_key, model, api_url=''):
    """Factory: create a provider instance from config values."""
    provider = (provider or 'anthropic').strip().lower()
    if provider == 'anthropic':
        return AnthropicProvider(api_key, model, api_url)
    return OpenAICompatProvider(api_key, model, api_url)


# ── Anthropic provider (faithful curie port) ────────────────────────────────────

class AnthropicProvider:
    name = 'anthropic'
    chunk_token_budget = ANTHROPIC_CHUNK_TOKEN_BUDGET
    models = ANTHROPIC_MODELS
    # Reasoning models (DeepSeek) can burn the whole output budget on hidden
    # thinking blocks and then return no text block at all; disable thinking
    # and raise the token cap when talking to one. 16k/64k also leaves room
    # for the mention-assignment table produced by the enrich pass.
    DEFAULT_MAX_TOKENS = 16000
    DEEPSEEK_MAX_TOKENS = 64000

    def __init__(self, api_key, model, api_url=''):
        self.api_key = api_key
        self.model = model or 'claude-sonnet-4-6'
        url = (api_url or ANTHROPIC_DEFAULT_URL).strip().rstrip('/')
        host = urlparse(url).hostname or ''
        self._is_deepseek = 'deepseek' in host.lower()
        if self._is_deepseek and not url.endswith('/anthropic'):
            # DeepSeek's Anthropic-compatible endpoint lives under
            # /anthropic; auto-fix bare hosts (https://api.deepseek.com)
            # so misconfigured URLs fail fast at test time instead of
            # producing confusing 404s.
            url = url + '/anthropic'
        self.api_url = url + '/v1/messages' if not url.endswith('/v1/messages') else url
        self.max_tokens = self.DEEPSEEK_MAX_TOKENS if self._is_deepseek else self.DEFAULT_MAX_TOKENS

    def _extra_payload(self):
        if self._is_deepseek:
            return {'thinking': {'type': 'disabled'}}
        return {}

    def _headers(self, betas=None):
        h = {
            'x-api-key': self.api_key,
            # DeepSeek's Anthropic-compatible endpoint (https://api.deepseek.com/anthropic)
            # accepts the same headers as Anthropic, and also honours Bearer auth;
            # sending both keeps the provider compatible with either service.
            'Authorization': f'Bearer {self.api_key}',
            'anthropic-version': API_VERSION,
            'content-type': 'application/json',
        }
        if betas:
            h['anthropic-beta'] = ','.join(betas)
        return h

    def _usage_from(self, resp):
        u = resp.get('usage', {})
        return {
            'input_tokens': u.get('input_tokens', 0),
            'output_tokens': u.get('output_tokens', 0),
            'cache_write_tokens': u.get('cache_creation_input_tokens', 0),
            'cache_read_tokens': u.get('cache_read_input_tokens', 0),
        }

    def calc_cost(self, usage):
        if not usage:
            return 0.0
        p = self.models.get(self.model, self.models['claude-sonnet-4-6'])
        return (
            usage.get('input_tokens', 0) * p['input']
            + usage.get('output_tokens', 0) * p['output']
            + usage.get('cache_write_tokens', 0) * p['cache_write']
            + usage.get('cache_read_tokens', 0) * p['cache_read']
        ) / 1_000_000

    def estimate_cost(self, usage1, usage2):
        p = self.models.get(self.model, self.models['claude-sonnet-4-6'])
        est1 = sum(p['estimate_step1'])
        est2 = sum(p['estimate_step2'])
        return round(est1 + est2, 2)

    def wait_for_rate_limit(self, resp_headers, tokens_needed, progress_cb=None):
        """Proactively wait if the remaining token budget won't cover the next request."""
        try:
            remaining = int(resp_headers.get('anthropic-ratelimit-tokens-remaining', 999_999))
        except (ValueError, TypeError):
            return
        if remaining >= tokens_needed:
            return
        reset_str = resp_headers.get('anthropic-ratelimit-tokens-reset', '')
        wait = 0
        if reset_str:
            try:
                reset_time = datetime.fromisoformat(reset_str.replace('Z', '+00:00'))
                wait = max(0, (reset_time - datetime.now(timezone.utc)).total_seconds()) + 1
            except Exception:
                wait = 60
        if wait > 0:
            _countdown_sleep(int(wait), 'Rate limit — pausing before Step 2', progress_cb)

    # ── Step 1: web research ────────────────────────────────────────────────

    def generate_book_data(self, title, author, include_characters,
                           include_places, language, progress_cb=None):
        prompt = build_step1_prompt(title, author, include_characters,
                                    include_places, language, with_search=True)
        messages = [{'role': 'user', 'content': prompt}]
        total_usage = {'input_tokens': 0, 'output_tokens': 0,
                       'cache_write_tokens': 0, 'cache_read_tokens': 0}
        last_headers = {}

        step1_attempt = 0
        while True:
            if progress_cb:
                progress_cb('Step 1: Searching online for book data…')
            try:
                payload = {
                    'model': self.model,
                    'max_tokens': self.max_tokens,
                    'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 5}],
                    'messages': messages,
                }
                payload.update(self._extra_payload())
                resp, last_headers = _http_post(self.api_url, self._headers(['web-search-2025-03-05']), payload)
            except Exception as exc:
                if _wait_429(exc, step1_attempt, progress_cb):
                    step1_attempt += 1
                    continue
                raise
            step1_attempt = 0

            for k in total_usage:
                total_usage[k] += self._usage_from(resp).get(k, 0)

            stop_reason = resp.get('stop_reason')
            content = resp.get('content', [])

            if stop_reason == 'end_turn':
                text_blocks = [
                    b['text'] for b in content
                    if b.get('type') == 'text' and b.get('text', '').strip()
                ]
                if text_blocks:
                    return extract_json(text_blocks[-1]), total_usage, last_headers
                raise ValueError('Step 1: no text block in response')

            if stop_reason == 'tool_use':
                messages.append({'role': 'assistant', 'content': content})
                messages.append({'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': b['id'], 'content': ''}
                    for b in content if b.get('type') == 'tool_use'
                ]})
            else:
                raise ValueError(f'Step 1: unexpected stop_reason "{stop_reason}"')

    # ── Step 2: EPUB enrichment with prompt caching ─────────────────────────

    def enrich_book_data(self, book_data, epub_text, ambiguous_names=None,
                         progress_cb=None, chunk_label=''):
        if progress_cb:
            suffix = f' ({chunk_label})' if chunk_label else ''
            progress_cb(f'Step 2 (EPUB spoiler analysis): Enriching with Claude{suffix}…')

        payload = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'system': [
                {
                    'type': 'text',
                    'text': (
                        'You are an expert literary analyst. '
                        'Below is the book text (or an excerpt), organized by '
                        'chapter with [pN] paragraph markers.\n\n'
                        + epub_text
                    ),
                    'cache_control': {'type': 'ephemeral'},
                }
            ],
            'messages': [
                {
                    'role': 'user',
                    'content': build_enrich_prompt(
                        book_data, epub_text, '', ambiguous_names),
                }
            ],
        }
        payload.update(self._extra_payload())

        resp = None
        for attempt in range(3):
            try:
                resp, _ = _http_post(self.api_url, self._headers(), payload)
                break
            except Exception as exc:
                if not _wait_429(exc, attempt, progress_cb):
                    raise

        if resp is None:
            raise ValueError(
                'Step 2: no response from API (rate-limited 3 times)')

        usage = self._usage_from(resp)
        for block in resp.get('content', []):
            if block.get('type') == 'text' and block.get('text', '').strip():
                parsed = extract_json(block['text'])
                chunk_mentions = parsed.get('mentions', []) if isinstance(parsed, dict) else []
                return _split_enrich_result(parsed), usage, chunk_mentions

        raise ValueError(
            f'Step 2: no text block in Claude response '
            f'(stop_reason={resp.get("stop_reason")}, '
            f'blocks={[b.get("type") for b in resp.get("content", [])]})'
        )

    # ── Step 4: spoiler audit ──────────────────────────────────────────────

    def review_spoilers(self, book_data, material, entries,
                        progress_cb=None, chunk_label=''):
        if progress_cb:
            suffix = f' ({chunk_label})' if chunk_label else ''
            progress_cb(f'Step 4 (spoiler audit): Auditing with Claude{suffix}…')

        payload = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'system': [
                {
                    'type': 'text',
                    'text': (
                        'You are a meticulous spoiler auditor for literary fiction. '
                        'Below is LATER-book material — text from chapters AFTER the '
                        'characters first appear. This is the spoiler source.\n\n'
                        + material
                    ),
                    'cache_control': {'type': 'ephemeral'},
                }
            ],
            'messages': [
                {
                    'role': 'user',
                    'content': build_review_prompt(book_data, entries),
                }
            ],
        }
        payload.update(self._extra_payload())

        resp = None
        for attempt in range(3):
            try:
                resp, _ = _http_post(self.api_url, self._headers(), payload)
                break
            except Exception as exc:
                if not _wait_429(exc, attempt, progress_cb):
                    raise

        if resp is None:
            raise ValueError(
                'Step 4: no response from API (rate-limited 3 times)')

        for block in resp.get('content', []):
            if block.get('type') == 'text' and block.get('text', '').strip():
                return extract_json(block['text'])

        raise ValueError(
            f'Step 4: no text block in Claude response '
            f'(stop_reason={resp.get("stop_reason")}, '
            f'blocks={[b.get("type") for b in resp.get("content", [])]})'
        )


# ── OpenAI-compatible provider (DeepSeek / Qwen / SiliconFlow …) ────────────────

class OpenAICompatProvider:
    name = 'openai_compat'
    chunk_token_budget = OPENAI_COMPAT_CHUNK_TOKEN_BUDGET
    default_pricing = {'input': 0.27, 'output': 1.10}
    # Output budget for enrich/review passes (deepseek-chat class models).
    # Step 1 uses a fixed small budget (see generate_book_data).
    DEFAULT_MAX_TOKENS = 16000

    def __init__(self, api_key, model, api_url=''):
        self.api_key = api_key
        self.model = model or 'deepseek-chat'
        self.max_tokens = self.DEFAULT_MAX_TOKENS
        url = (api_url or OPENAI_COMPAT_DEFAULT_URL).strip().rstrip('/')
        if url.endswith('/chat/completions'):
            self.api_url = url
        else:
            base = url + '/v1' if not url.endswith('/v1') else url
            self.api_url = base + '/chat/completions'

    def _headers(self):
        return {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }

    def _usage_from(self, resp):
        u = resp.get('usage', {}) or {}
        return {
            'input_tokens': u.get('prompt_tokens', 0),
            'output_tokens': u.get('completion_tokens', 0),
            'cache_write_tokens': 0,
            'cache_read_tokens': 0,
        }

    def calc_cost(self, usage):
        if not usage:
            return 0.0
        p = OPENAI_COMPAT_MODEL_PRICING.get(self.model, self.default_pricing)
        return (
            usage.get('input_tokens', 0) * p['input']
            + usage.get('output_tokens', 0) * p['output']
        ) / 1_000_000

    def estimate_cost(self, usage1, usage2):
        """Rough estimate: 60k input + 4k output tokens total."""
        p = OPENAI_COMPAT_MODEL_PRICING.get(self.model, self.default_pricing)
        return round((60_000 * p['input'] + 4_000 * p['output']) / 1_000_000, 2)

    def wait_for_rate_limit(self, resp_headers, tokens_needed, progress_cb=None):
        return

    # ── Step 1: knowledge-based (no web search) ─────────────────────────────

    def generate_book_data(self, title, author, include_characters,
                           include_places, language, progress_cb=None):
        prompt = build_step1_prompt(title, author, include_characters,
                                    include_places, language, with_search=False)
        payload = {
            'model': self.model,
            'max_tokens': 8000,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if progress_cb:
            progress_cb('Step 1: Generating book data (knowledge mode, no web search)…')

        resp = None
        for attempt in range(3):
            try:
                resp, _ = _http_post(self.api_url, self._headers(), payload)
                break
            except Exception as exc:
                if not _wait_429(exc, attempt, progress_cb):
                    raise
        if resp is None:
            raise ValueError('Step 1: no response from API')

        content = (resp.get('choices') or [{}])[0].get('message', {}).get('content', '')
        if not content:
            raise ValueError('Step 1: empty response content')
        return extract_json(content), self._usage_from(resp), {}

    # ── Step 2: EPUB enrichment (no caching support) ─────────────────────────

    def enrich_book_data(self, book_data, epub_text, ambiguous_names=None,
                         progress_cb=None, chunk_label=''):
        if progress_cb:
            suffix = f' ({chunk_label})' if chunk_label else ''
            progress_cb(f'Step 2 (EPUB spoiler analysis): Enriching with {self.model}{suffix}…')

        payload = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        'You are an expert literary analyst. '
                        'Below is the book text (or an excerpt), organized by '
                        'chapter with [pN] paragraph markers.\n\n'
                        + epub_text
                    ),
                },
                {
                    'role': 'user',
                    'content': build_enrich_prompt(
                        book_data, epub_text, '', ambiguous_names),
                },
            ],
        }

        resp = None
        for attempt in range(3):
            try:
                resp, _ = _http_post(self.api_url, self._headers(), payload)
                break
            except Exception as exc:
                if not _wait_429(exc, attempt, progress_cb):
                    raise
        if resp is None:
            raise ValueError('Step 2: no response from API')

        content = (resp.get('choices') or [{}])[0].get('message', {}).get('content', '')
        if not content:
            raise ValueError('Step 2: empty response content')
        parsed = extract_json(content)
        chunk_mentions = parsed.get('mentions', []) if isinstance(parsed, dict) else []
        return _split_enrich_result(parsed), self._usage_from(resp), chunk_mentions

    # ── Step 4: spoiler audit ──────────────────────────────────────────────

    def review_spoilers(self, book_data, material, entries,
                        progress_cb=None, chunk_label=''):
        if progress_cb:
            suffix = f' ({chunk_label})' if chunk_label else ''
            progress_cb(f'Step 4 (spoiler audit): Auditing with {self.model}{suffix}…')

        payload = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        'You are a meticulous spoiler auditor for literary fiction. '
                        'Below is LATER-book material — text from chapters AFTER the '
                        'characters first appear. This is the spoiler source.\n\n'
                        + material
                    ),
                },
                {
                    'role': 'user',
                    'content': build_review_prompt(book_data, entries),
                },
            ],
        }

        resp = None
        for attempt in range(3):
            try:
                resp, _ = _http_post(self.api_url, self._headers(), payload)
                break
            except Exception as exc:
                if not _wait_429(exc, attempt, progress_cb):
                    raise
        if resp is None:
            raise ValueError('Step 4: no response from API')

        content = (resp.get('choices') or [{}])[0].get('message', {}).get('content', '')
        if not content:
            raise ValueError('Step 4: empty response content')
        return extract_json(content)


# ── Small test-call helper (used by "test connection") ─────────────────────────

def test_connection(provider, api_key, model, api_url='', progress_cb=None):
    """Do a tiny LLM call to verify the API key + endpoint work."""
    prov = make_provider(provider, api_key, model, api_url)
    if isinstance(prov, AnthropicProvider):
        payload = {
            'model': prov.model,
            'max_tokens': 32,
            'messages': [{'role': 'user', 'content': 'Reply with exactly: OK'}],
        }
        resp, _ = _http_post(prov.api_url, prov._headers(), payload, timeout=60)
        text_blocks = [
            b['text'] for b in resp.get('content', [])
            if b.get('type') == 'text' and b.get('text', '').strip()
        ]
        if not text_blocks:
            raise ValueError('Empty response from Anthropic API')
        return text_blocks[0]
    else:
        payload = {
            'model': prov.model,
            'max_tokens': 32,
            'messages': [{'role': 'user', 'content': 'Reply with exactly: OK'}],
        }
        resp, _ = _http_post(prov.api_url, prov._headers(), payload, timeout=60)
        content = (resp.get('choices') or [{}])[0].get('message', {}).get('content', '')
        if not content:
            raise ValueError('Empty response from API')
        return content
