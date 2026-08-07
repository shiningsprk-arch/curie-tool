"""Live end-to-end test against a real Anthropic-compatible API.

Reads the API key ONLY from the environment (never hardcoded / never written
to disk). Run:

    $env:DEEPSEEK_API_KEY = "..."
    python tests/test_deepseek_live.py <in.epub> <out.epub> [--url URL] [--model MODEL] [--lang LANG]

Example:
    python tests/test_deepseek_live.py "夏帆.epub" "夏帆（Curie导读版）.epub" \
        --url https://api.deepseek.com/anthropic --model deepseek-v4-flash --lang Chinese
"""

import argparse
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from webserver.toolbox.curie import pipeline  # noqa: E402
from webserver.toolbox.curie.api_client import AnthropicProvider  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("in_epub")
    parser.add_argument("out_epub")
    parser.add_argument("--url", default="https://api.deepseek.com/anthropic")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--lang", default="Chinese")
    parser.add_argument("--characters", action="store_true", default=True)
    parser.add_argument("--places", action="store_true", default=False)
    parser.add_argument("--density", default="every_10_paragraphs")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("CURIE_API_KEY")
    if not api_key:
        print("ERROR: set DEEPSEEK_API_KEY env var first")
        return 1

    if not os.path.exists(args.in_epub):
        print(f"ERROR: input epub not found: {args.in_epub}")
        return 1

    work_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "curie_live")
    os.makedirs(work_dir, exist_ok=True)
    src_copy = os.path.join(work_dir, "source.epub")
    data_path = os.path.join(work_dir, "book_data.json")
    shutil.copy2(args.in_epub, src_copy)

    provider = AnthropicProvider(api_key, args.model, args.url)
    print(f"provider: {provider.name} url={provider.api_url} model={provider.model}")

    def progress(msg):
        print(f"  [{time.strftime('%H:%M:%S')}] {msg}")

    t0 = time.time()
    print("== Step 1+2: analysis ==")
    book_data = pipeline.run_curie_analysis(
        provider, os.path.basename(args.in_epub), "", src_copy,
        include_characters=args.characters, include_places=args.places,
        language=args.lang, output_path=data_path,
        progress_cb=progress,
    )
    print(f"analysis done in {time.time() - t0:.1f}s")
    print(f"characters: {len(book_data.get('characters', []))}")
    for c in book_data.get("characters", []):
        print(f"  - {c.get('full_name')} ({c.get('role')}) ch{c.get('chapter')} x{c.get('occurrences')}")
    print(f"locations: {len(book_data.get('locations', []))}")
    for loc in book_data.get("locations", []):
        print(f"  - {loc.get('name')} ({loc.get('type')}) ch{loc.get('chapter')} x{loc.get('occurrences')}")

    print("== Step 3: inject ==")
    stats = pipeline.build_curied_epub(src_copy, args.out_epub, book_data, args.density, progress)
    print(f"inject stats: {stats}")
    print(f"output written: {args.out_epub} ({os.path.getsize(args.out_epub)} bytes)")
    print(f"book_data.json saved: {data_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
