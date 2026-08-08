"""Unit tests for the ported Curie core (runs standalone, no MyBooks deps).

Usage: python tests/test_curie_core.py   (or pytest tests/)
"""

import os
import sys
import tempfile
import zipfile

# Allow running from the repo root without installing the webserver package
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.abspath(os.path.join(_HERE, ".."))
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from webserver.toolbox.curie import epub_injector, epub_utils, pipeline  # noqa: E402

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Ch1</title></head>
<body>
<p>It was a fine day. Alice Liddell walked through the garden.</p>
<p>Somewhere far away, Wonderland awaited.</p>
</body>
</html>
"""

CH2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Ch2</title></head>
<body>
<p>The Hatter poured the tea and smiled at Alice Liddell.</p>
<p>Ali laughed. Wonderland was full of surprises.</p>
</body>
</html>
"""

CH3 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Ch3</title></head>
<body>
<p>The Hatter asked Alice to stay a little longer, promising that the tea would not run out.</p>
<p>Alice smiled and sat back down, watching the Hatter pour another cup of steaming tea.</p>
</body>
</html>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:test</dc:identifier>
    <dc:title>Test Book</dc:title>
  </metadata>
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch3" href="ch3.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
    <itemref idref="ch3"/>
  </spine>
</package>
"""

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

BOOK_DATA = {
    "title": "Test Book",
    "author": "Author",
    "characters": [
        {"full_name": "Alice Liddell", "nicknames": ["Ali"], "role": "protagonist",
         "description": "A curious girl who falls down a rabbit hole.", "chapter": 1},
        {"full_name": "The Hatter", "nicknames": [], "role": "major character",
         "description": "A mad hatter who loves tea.", "chapter": 2},
    ],
    "locations": [
        {"name": "Wonderland", "type": "landmark", "aliases": [],
         "description": "A dreamy place full of surprises.", "chapter": 1},
    ],
}


def build_sample_epub(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER)
        zf.writestr("OEBPS/content.opf", OPF)
        zf.writestr("OEBPS/ch1.xhtml", CH1)
        zf.writestr("OEBPS/ch2.xhtml", CH2)
        zf.writestr("OEBPS/ch3.xhtml", CH3)


def zip_names(path):
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def read_zip(path, name):
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode("utf-8")


def first_zip_name(path):
    with zipfile.ZipFile(path) as zf:
        return zf.infolist()[0].filename


def test_spine_extraction():
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        build_sample_epub(epub)
        chapters = epub_utils.extract_spine_texts(epub)
        assert len(chapters) == 3, f"expected 3 chapters, got {len(chapters)}"
        nums = [n for n, _ in chapters]
        assert nums == [1, 2, 3]
        texts = {n: t for n, t in chapters}
        assert "Alice Liddell" in texts[1]
        assert "Wonderland" in texts[2]


def test_chunking():
    chapters = [(1, "A" * 100000), (2, "B" * 100000)]
    chunks = epub_utils.build_epub_chunks(chapters, chunk_token_budget=40_000)
    assert len(chunks) >= 2, f"expected multiple chunks, got {len(chunks)}"
    small = [(1, "short text"), (2, "more text")]
    assert len(epub_utils.build_epub_chunks(small)) == 1


def test_inject_footnotes():
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        build_sample_epub(epub)
        original = read_zip(epub, "OEBPS/ch2.xhtml")

        stats = epub_injector.inject_footnotes(epub, BOOK_DATA, hint_density="every_mention")
        assert stats["chapters_modified"] >= 2, stats
        assert stats["refs_injected"] >= 3, stats

        names = zip_names(epub)
        for f in ("OEBPS/curie-char-1-v1.xhtml", "OEBPS/curie-char-2-v1.xhtml",
                  "OEBPS/curie-loc-1-v1.xhtml"):
            assert f in names, f"missing {f} in {names}"

        # OPF updated: manifest items + linear=no itemrefs
        opf = read_zip(epub, "OEBPS/content.opf")
        assert 'id="curie-char-1-v1"' in opf
        assert 'idref="curie-char-1-v1" linear="no"' in opf

        # chapter 2 now carries noteref links
        ch2 = read_zip(epub, "OEBPS/ch2.xhtml")
        assert 'epub:type="noteref"' in ch2
        assert 'curie-char-1-v1.xhtml#curie-char-1-v1' in ch2
        assert 'data-curie="true"' in ch2

        # mimetype must remain first & uncompressed
        assert first_zip_name(epub) == "mimetype"

        # idempotency: re-inject must not double up
        stats2 = epub_injector.inject_footnotes(epub, BOOK_DATA, hint_density="every_mention")
        ch2b = read_zip(epub, "OEBPS/ch2.xhtml")
        assert ch2b.count('curie-char-1-v1.xhtml#curie-char-1-v1') == ch2.count('curie-char-1-v1.xhtml#curie-char-1-v1')
        assert stats2["refs_injected"] == stats["refs_injected"]

        # restore (xmlns:epub declaration added during injection is harmless
        # and intentionally kept — same as upstream curie)
        removed = epub_injector.remove_injections(epub)
        assert removed["chapters_cleaned"] >= 2, removed
        names_after = zip_names(epub)
        assert not any("curie-" in n for n in names_after), names_after
        restored = read_zip(epub, "OEBPS/ch2.xhtml").replace(
            ' xmlns:epub="http://www.idpf.org/2007/ops"', '')
        assert restored == original


def test_remove_without_injection_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        build_sample_epub(epub)
        removed = epub_injector.remove_injections(epub)
        assert removed["chapters_cleaned"] == 0


def test_post_processing():
    data = {
        "characters": [
            {"full_name": "Alice", "nicknames": ["ali", "bad lower", "Ali", "三哥"],
             "description": "x", "chapter": 1, "occurrences": 5},
            {"wrong_field": "Ghost"},  # dropped by _fix_schema
            {"full_name": "Barely", "nicknames": [], "description": "y",
             "occurrences": 1},  # filtered by min occurrences
        ],
        "locations": [
            {"name": "Home", "type": "residential", "aliases": ["home base", "老屋"],
             "description": "z", "chapter": 1, "occurrences": 4},
        ],
    }
    pipeline._fix_schema(data)
    pipeline._filter_aliases(data)
    pipeline._filter_by_occurrences(data)
    chars = data["characters"]
    assert len(chars) == 1
    assert chars[0]["full_name"] == "Alice"
    assert chars[0]["nicknames"] == ["Ali", "三哥"]  # Latin upper + CJK kept
    assert data["locations"][0]["aliases"] == ["老屋"]  # "home base" is subset of primary


def test_add_chapter_and_occurrences():
    chapters = [(1, "Alice went home"), (2, "Alice and Ali came back")]
    data = {
        "characters": [{"full_name": "Alice", "nicknames": ["Ali"], "description": "x"}],
        "locations": [],
    }
    pipeline._add_chapter_and_occurrences(data, chapters)
    c = data["characters"][0]
    assert c["chapter"] == 1
    assert c["occurrences"] == 3  # Alice x2 + Ali x1


class _MockProvider:
    """Fake provider implementing the provider interface without API calls."""

    name = "mock"
    chunk_token_budget = 160_000

    def __init__(self, review_fixes=None, mentions=None):
        self.calls = []
        self.review_fixes = review_fixes or {}
        self.mentions = mentions or []

    def generate_book_data(self, title, author, include_characters,
                           include_places, language, progress_cb=None):
        self.calls.append("generate")
        data = {"title": title, "author": author,
                "characters": [
                    {"full_name": "Alice Liddell", "nicknames": ["Ali"],
                     "role": "protagonist", "description": "A curious girl."},
                    {"full_name": "The Hatter", "nicknames": [],
                     "role": "major character", "description": "A mad hatter."},
                ],
                "locations": [
                    {"name": "Wonderland", "type": "landmark", "aliases": [],
                     "description": "A dreamy place."},
                ]}
        if not include_places:
            data["locations"] = []
        return data, {"input_tokens": 100, "output_tokens": 100}, {}

    def enrich_book_data(self, book_data, epub_text, ambiguous_names=None,
                         progress_cb=None, chunk_label=""):
        self.calls.append("enrich")
        return book_data, {"input_tokens": 50, "output_tokens": 50}, self.mentions

    def review_spoilers(self, book_data, material, entries,
                        progress_cb=None, chunk_label=""):
        self.calls.append("review")
        return self.review_fixes

    def wait_for_rate_limit(self, resp_headers, tokens_needed, progress_cb=None):
        return

    def calc_cost(self, usage):
        return 0.0


def test_pipeline_e2e_with_mock():
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        build_sample_epub(epub)
        out = os.path.join(tmp, "book_data.json")
        provider = _MockProvider()
        progress_msgs = []

        data = pipeline.run_curie_analysis(
            provider, "Test Book", "Author", epub,
            include_characters=True, include_places=True,
            language="English", output_path=out,
            progress_cb=lambda m: progress_msgs.append(m),
        )
        assert provider.calls == ["generate", "enrich", "review"], provider.calls
        assert os.path.exists(out)

        # Alice: ch1(1) + ch2(2 with Ali) = 3 occurrences -> kept
        alice = [c for c in data["characters"] if c["full_name"] == "Alice Liddell"]
        assert len(alice) == 1
        assert alice[0]["chapter"] == 1
        assert alice[0]["occurrences"] >= 3

        # The Hatter: ch2(1) + ch3(2) = 3 occurrences -> kept, first at ch2
        hatter = [c for c in data["characters"] if c["full_name"] == "The Hatter"]
        assert len(hatter) == 1
        assert hatter[0]["chapter"] == 2

        # Wonderland: only 2 occurrences -> filtered out (min 3)
        assert data["locations"] == []

        # reload from disk works
        assert pipeline.load_book_data(out) == data
        assert pipeline.load_book_data(os.path.join(tmp, "missing.json")) is None


def test_review_for_spoilers_applies_fixes():
    """Step 4 fixes descriptions flagged as spoiling and applies timelines."""
    book_data = {
        "title": "T", "author": "A",
        "characters": [
            {"full_name": "Alice", "nicknames": [], "role": "protagonist",
             "description": "She later becomes the Queen of Hearts.", "chapter": 1,
             "occurrences": 5},
            {"full_name": "Bob", "nicknames": [], "role": "minor",
             "description": "A quiet carpenter.", "chapter": 2, "occurrences": 4},
        ],
        "locations": [],
    }
    chapters = [(1, "Alice walked here. " * 60), (2, "Bob worked there. " * 60),
                (3, "Alice and Bob both appeared later. " * 60)]

    provider = _MockProvider(review_fixes={
        "characters": [
            {"full_name": "Alice", "description": "A curious girl.",
             "timeline": [
                 {"up_to": 1, "description": "A curious girl."},
                 {"up_to": 3, "description": "A curious girl who rules."},
             ]},
        ],
        "locations": [],
    })
    pipeline.review_for_spoilers(provider, book_data, chapters)

    alice = [c for c in book_data["characters"] if c["full_name"] == "Alice"][0]
    bob = [c for c in book_data["characters"] if c["full_name"] == "Bob"][0]
    assert alice["description"] == "A curious girl."  # fixed
    assert alice["timeline"] == [
        {"up_to": 1, "description": "A curious girl."},
        {"up_to": 3, "description": "A curious girl."},
    ]  # last stage forced to match final description
    assert bob["description"] == "A quiet carpenter."  # untouched
    assert "timeline" not in bob


def test_review_for_spoilers_no_material_noop():
    """Entities with no later-chapter material are skipped (no review call)."""
    book_data = {
        "title": "T", "author": "A",
        "characters": [
            {"full_name": "Solo", "nicknames": [], "role": "protagonist",
             "description": "Only appears in chapter 1.", "chapter": 1, "occurrences": 3},
        ],
        "locations": [],
    }
    chapters = [(1, "Solo was here. " * 60), (2, "Nobody else. " * 60)]
    provider = _MockProvider()
    pipeline.review_for_spoilers(provider, book_data, chapters)
    assert provider.calls == []  # no material -> no API call at all
    assert book_data["characters"][0]["description"] == "Only appears in chapter 1."


def test_timeline_stage_selection():
    """Links point to the right progressive stage for the current chapter."""
    from webserver.toolbox.curie.epub_injector import _timeline_stage

    entity = {
        "timeline": [
            {"up_to": 6, "description": "v1: early"},
            {"up_to": 12, "description": "v2: middle"},
            {"up_to": 21, "description": "v3: full"},
        ]
    }
    assert _timeline_stage(entity, 1) == (0, entity["timeline"][0])
    assert _timeline_stage(entity, 6) == (0, entity["timeline"][0])
    assert _timeline_stage(entity, 7) == (1, entity["timeline"][1])
    assert _timeline_stage(entity, 13) == (2, entity["timeline"][2])
    assert _timeline_stage(entity, 21) == (2, entity["timeline"][2])
    assert _timeline_stage(entity, 99) == (2, entity["timeline"][2])
    # no timeline -> fallback
    assert _timeline_stage({"description": "x"}, 5) == (None, None)


def test_inject_timeline_progressive():
    """Chapters before/after a timeline boundary link to different stage files."""
    data = {
        "characters": [
            {"full_name": "Alice Liddell", "nicknames": ["Alice"], "role": "protagonist",
             "description": "v2: the queen.", "chapter": 0,
             "timeline": [{"up_to": 1, "description": "v1: a curious girl."},
                          {"up_to": 3, "description": "v2: the queen."}]},
        ],
        "locations": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        build_sample_epub(epub)
        epub_injector.inject_footnotes(epub, data, hint_density="every_mention")

        names = zip_names(epub)
        # ch1 links to v1; ch2/ch3 link to v2 (chapter=0 -> no first-chapter skip)
        ch1 = read_zip(epub, "OEBPS/ch1.xhtml")
        ch2 = read_zip(epub, "OEBPS/ch2.xhtml")
        ch3 = read_zip(epub, "OEBPS/ch3.xhtml")
        assert "curie-char-1-v1.xhtml#curie-char-1-v1" in ch1
        assert "curie-char-1-v2.xhtml#curie-char-1-v2" in ch2
        assert "curie-char-1-v2.xhtml#curie-char-1-v2" in ch3
        assert "OEBPS/curie-char-1-v1.xhtml" in names
        assert "OEBPS/curie-char-1-v2.xhtml" in names
        # stage files carry the matching description
        v1 = read_zip(epub, "OEBPS/curie-char-1-v1.xhtml")
        v2 = read_zip(epub, "OEBPS/curie-char-1-v2.xhtml")
        assert "a curious girl" in v1
        assert "the queen" in v2
        # idempotent re-inject + full restore
        epub_injector.inject_footnotes(epub, data, hint_density="every_mention")
        removed = epub_injector.remove_injections(epub)
        assert removed["chapters_cleaned"] >= 1
        names_after = zip_names(epub)
        assert not any("curie-" in n for n in names_after)


def test_cjk_name_boundaries():
    """CJK books are the main use case — names must match inside running text."""
    cases = [
        ("张三", "张三走进房间", True),
        ("张三", "她说张三很好", True),
        ("三", "刘三", False),           # single CJK char inside longer name
        ("三", "三哥说", True),
        ("Alice", "Alice笑着说", True),   # Latin + CJK mixing
        ("Alice", "MyAlice", False),
        ("Alice Liddell", "Alice Liddell walked", True),
        ("兔子洞", "兔子洞就在前方", True),
        ("Hatter", "the Hatter poured tea", True),
    ]
    for name, text, expected in cases:
        got = bool(epub_utils.build_name_pattern(name).search(text))
        assert got == expected, f"pattern({name!r}) on {text!r}: got {got}, want {expected}"


def test_inject_cjk_book():
    """Full inject cycle on a Chinese chapter."""
    zh_ch = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>第一章</title></head>
<body>
<p>张三走进房间，对她说：“阿强来了。”她放下手里的书，抬头看着张三。</p>
<p>她说张三很好，兔子洞就在前方等着他们。张三点点头，把阿强的事情细细说了一遍。</p>
<p>屋外的风很大，张三关上了窗户，又给阿强倒了一杯热茶，两人聊到深夜。</p>
</body>
</html>
"""
    data = {
        "characters": [
            {"full_name": "张三", "nicknames": ["阿强"], "role": "protagonist",
             "description": "主角。", "chapter": 0},
        ],
        "locations": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "zh.epub")
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                        compress_type=zipfile.ZIP_STORED)
            zf.writestr("META-INF/container.xml", CONTAINER)
            opf = OPF.replace("ch1.xhtml", "c1.xhtml").replace("ch2.xhtml", "c1.xhtml").replace("ch3.xhtml", "c1.xhtml")
            zf.writestr("OEBPS/content.opf", opf)
            zf.writestr("OEBPS/c1.xhtml", zh_ch)

        stats = epub_injector.inject_footnotes(epub, data, hint_density="every_mention")
        assert stats["refs_injected"] >= 2, stats  # 张三 x2 + 阿强 x1
        html = read_zip(epub, "OEBPS/c1.xhtml")
        assert html.count("curie-char-1-v1.xhtml#curie-char-1-v1") >= 2


def test_extract_json_tolerates_noise():
    """LLM responses may include fences/prose/extra JSON — always take the last object."""
    from webserver.toolbox.curie.api_client import extract_json

    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"b": 2} hope this helps') == {"b": 2}
    # multiple objects -> last one wins
    assert extract_json('{"c": 3}\n{"d": 4}') == {"d": 4}
    assert extract_json('prefix {"e": {"f": 5}} suffix') == {"e": {"f": 5}}


def test_dedupe_entities():
    """Exact-duplicate entities get merged (nicknames union, best desc/timeline)."""
    data = {
        "characters": [
            {"full_name": "何塞·阿尔卡蒂奥", "nicknames": ["何塞"], "role": "major",
             "description": "短描述", "occurrences": 3},
            {"full_name": "何塞·阿尔卡蒂奥", "nicknames": ["何塞", "阿尔卡蒂奥"],
             "role": "major", "description": "这是一条更长的完整描述，包含更多信息。",
             "occurrences": 9, "timeline": [{"up_to": 5, "description": "x"}]},
            {"full_name": "其他人", "nicknames": [], "role": "minor", "description": "y"},
        ],
        "locations": [],
    }
    pipeline._dedupe_entities(data)
    chars = data["characters"]
    assert len(chars) == 2
    merged = [c for c in chars if c["full_name"] == "何塞·阿尔卡蒂奥"][0]
    assert set(merged["nicknames"]) == {"何塞", "阿尔卡蒂奥"}
    assert merged["description"] == "这是一条更长的完整描述，包含更多信息。"
    assert merged["timeline"] == [{"up_to": 5, "description": "x"}]


def test_active_ranges():
    """active_from/to derived from where the entity's names actually occur."""
    chapters = [(1, "A appeared here. " * 50), (2, "Nothing. " * 50),
                (3, "A came back and B joined. " * 50)]
    data = {
        "characters": [
            {"full_name": "A", "nicknames": [], "role": "x", "occurrences": 5},
            {"full_name": "B", "nicknames": [], "role": "x", "occurrences": 5},
        ],
        "locations": [],
    }
    pipeline._add_active_ranges(data, chapters)
    by_name = {c["full_name"]: c for c in data["characters"]}
    assert by_name["A"]["active_from"] == 1
    assert by_name["A"]["active_to"] == 3
    assert by_name["B"]["active_from"] == 3
    assert by_name["B"]["active_to"] == 3


def test_inject_shared_short_name_active_preference():
    """A shared short name is claimed by the entity active in that chapter."""
    ch1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Ch1</title></head>
<body>
<p>阿玛兰妲走进房间，说了一句什么。她放下手里的书，静静地看着窗外。</p>
<p>阿玛兰妲又补充了几句，然后离开了。屋子里只剩下钟摆的声音，一下又一下。</p>
<p>第二天清晨，阿玛兰妲早早起来，继续缝制她那件永远也缝不完的寿衣。</p>
</body>
</html>
"""
    ch2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Ch2</title></head>
<body>
<p>多年以后，阿玛兰妲从布鲁塞尔回到了马孔多。她推开老宅的大门，灰尘纷纷落下。</p>
<p>阿玛兰妲开始重新打理这座老宅，种上鲜花，修好围墙，整个镇子都听说了她的归来。</p>
<p>邻居们都说，阿玛兰妲变了，变得比从前更坚强，也更沉默。</p>
</body>
</html>
"""
    opf2 = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:test</dc:identifier>
    <dc:title>Test Book</dc:title>
  </metadata>
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""
    data = {
        "characters": [
            {"full_name": "阿玛兰妲·布恩迪亚", "nicknames": ["阿玛兰妲"], "role": "major",
             "description": "二代阿玛兰妲", "chapter": 0,
             "active_from": 1, "active_to": 1},
            {"full_name": "阿玛兰妲·乌尔苏拉", "nicknames": ["阿玛兰妲"], "role": "major",
             "description": "五代阿玛兰妲", "chapter": 0,
             "active_from": 2, "active_to": 2},
        ],
        "locations": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                        compress_type=zipfile.ZIP_STORED)
            zf.writestr("META-INF/container.xml", CONTAINER)
            zf.writestr("OEBPS/content.opf", opf2)
            zf.writestr("OEBPS/ch1.xhtml", ch1)
            zf.writestr("OEBPS/ch2.xhtml", ch2)

        epub_injector.inject_footnotes(epub, data, hint_density="every_mention")
        # ch1: entity 1 (active 1-1) injects first and claims 阿玛兰妲
        c1 = read_zip(epub, "OEBPS/ch1.xhtml")
        assert "curie-char-1-v1.xhtml#curie-char-1-v1" in c1
        # ch2: entity 1 inactive, entity 2 (active 2-2) claims it
        c2 = read_zip(epub, "OEBPS/ch2.xhtml")
        assert "curie-char-2-v1.xhtml#curie-char-2-v1" in c2
        # entity ids stay stable per entity (no cross-chapter id drift)
        names = zip_names(epub)
        assert "OEBPS/curie-char-1-v1.xhtml" in names
        assert "OEBPS/curie-char-2-v1.xhtml" in names


def test_to_llm_text_paragraphs():
    """Paragraph slots match block-level tags exactly (empty slots included)."""
    html = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>X</title></head>
<body>
<p>first</p>
<p></p>
<div>second<br/>line</div>
<li>item</li>
</body>
</html>
"""
    paras = epub_utils._to_llm_text(html)
    assert paras[0] == (1, "first")
    assert paras[1] == (2, "")          # empty <p> keeps its slot
    assert "second line" in paras[2][1]  # div + br merge in one slot
    assert paras[2][0] == 3
    assert paras[3] == (4, "item")
    # title in <head> must not leak into paragraphs
    assert not any("X" == t for _, t in paras)


def test_find_ambiguous_names():
    """Shared names and prefix names are flagged as ambiguous."""
    data = {
        "characters": [
            {"full_name": "奥雷里亚诺·布恩迪亚", "nicknames": ["奥雷里亚诺"], "role": "x"},
            {"full_name": "奥雷里亚诺·巴比伦", "nicknames": ["奥雷里亚诺"], "role": "x"},
            {"full_name": "阿玛兰妲·乌尔苏拉", "nicknames": [], "role": "x"},
            {"full_name": "乌尔苏拉", "nicknames": [], "role": "x"},
        ],
        "locations": [],
    }
    amb = pipeline._find_ambiguous_names(data)
    assert "奥雷里亚诺" in amb        # shared by two entities
    assert "阿玛兰妲·乌尔苏拉" not in amb  # not shared, not a prefix
    assert "乌尔苏拉" in amb          # prefix of 阿玛兰妲·乌尔苏拉
    assert "奥雷里亚诺·布恩迪亚" not in amb


def test_mentions_normalize():
    data = {"characters": [{"full_name": "A"}, {"full_name": "B"}], "locations": []}
    raw = [
        {"name": "奥雷里亚诺", "chapter": 3, "para": 5, "entity": "#2"},
        {"name": "奥雷里亚诺", "chapter": 3, "para": 5, "entity": "#2"},  # dup
        {"name": "坏条目", "chapter": "x", "para": 1, "entity": "#1"},    # bad chapter
        {"name": "", "chapter": 1, "para": 1, "entity": "#1"},            # empty name
        {"name": "越界", "chapter": 1, "para": 1, "entity": "#99"},       # bad index
        {"name": "奥雷里亚诺", "chapter": 4, "para": 9, "entity": "#1"},
    ]
    out = pipeline._normalize_mentions(raw, data)
    assert len(out) == 2
    assert out[0] == {"name": "奥雷里亚诺", "chapter": 3, "para": 5, "entity": 1}
    assert out[1]["entity"] == 0


def test_inject_mentions_paragraph_ownership():
    """Ambiguous-name mentions route the short name to the owning entity."""
    ch1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>C1</title></head>
<body>
<p>奥雷里亚诺上校走进房间，看着地图。桌上的油灯忽明忽暗，他沉默了很久，才缓缓开口。</p>
<p>他轻轻敲了敲桌子，门外的士兵应声而入，等待着这位传奇人物的命令。</p>
<p>多年以后，奥雷里亚诺·巴比伦才出生。他从小就被外婆藏在家里，对外面的世界一无所知。</p>
</body>
</html>
"""
    opf2 = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:test</dc:identifier>
    <dc:title>T</dc:title>
  </metadata>
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
  </spine>
</package>
"""
    data = {
        "characters": [
            {"full_name": "奥雷里亚诺·布恩迪亚", "nicknames": ["奥雷里亚诺", "奥雷里亚诺上校"],
             "role": "protagonist", "description": "上校", "chapter": 0},
            {"full_name": "奥雷里亚诺·巴比伦", "nicknames": ["奥雷里亚诺"],
             "role": "protagonist", "description": "巴比伦", "chapter": 0},
        ],
        "locations": [],
        "mentions": [
            {"name": "奥雷里亚诺", "chapter": 1, "para": 1, "entity": 0},
            {"name": "奥雷里亚诺", "chapter": 1, "para": 3, "entity": 1},
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                        compress_type=zipfile.ZIP_STORED)
            zf.writestr("META-INF/container.xml", CONTAINER)
            zf.writestr("OEBPS/content.opf", opf2)
            zf.writestr("OEBPS/ch1.xhtml", ch1)

        epub_injector.inject_footnotes(epub, data, hint_density="every_mention")
        html = read_zip(epub, "OEBPS/ch1.xhtml")
        # para 1: 奥雷里亚诺上校 -> entity 0 (full name match, not ambiguous path)
        assert "curie-char-1-v1.xhtml#curie-char-1-v1" in html
        # para 3: 奥雷里亚诺·巴比伦 must go to entity 1 (owned by mention)
        assert "curie-char-2-v1.xhtml#curie-char-2-v1" in html


def test_mentions_name_match_after_dedupe_filter():
    """Mentions survive dedupe/filter index drift by matching on name first."""
    data = {
        "characters": [
            {"full_name": "何塞·阿尔卡蒂奥", "nicknames": ["何塞"], "description": "短", "occurrences": 3},
            {"full_name": "何塞·阿尔卡蒂奥", "nicknames": ["何塞", "阿尔卡蒂奥"],
             "description": "较长的一条完整描述。", "occurrences": 9},
            {"full_name": "丽贝卡", "nicknames": [], "description": "路人", "occurrences": 1},
        ],
        "locations": [],
    }
    pipeline._dedupe_entities(data)
    pipeline._filter_by_occurrences(data)
    assert len(data["characters"]) == 1  # dup merged, 丽贝卡 filtered out

    raw = [
        # #2 pointed at the merged-away duplicate -> name match fixes to idx 0
        {"name": "何塞", "chapter": 1, "para": 2, "entity": "#2"},
        # 丽贝卡 no longer exists -> dropped
        {"name": "丽贝卡", "chapter": 1, "para": 3, "entity": "#3"},
    ]
    out = pipeline._normalize_mentions(raw, data)
    assert [m["entity"] for m in out] == [0]


def test_mentions_shared_name_uses_label_fallback():
    """Shared names (genuine ambiguity) keep positional '#N' resolution."""
    data = {
        "characters": [
            {"full_name": "奥雷里亚诺·布恩迪亚", "nicknames": ["奥雷里亚诺"], "occurrences": 5},
            {"full_name": "奥雷里亚诺·巴比伦", "nicknames": ["奥雷里亚诺"], "occurrences": 5},
            {"full_name": "乌尔苏拉", "nicknames": [], "occurrences": 1},
        ],
        "locations": [],
    }
    pipeline._filter_by_occurrences(data)
    assert len(data["characters"]) == 2  # 乌尔苏拉 filtered, shared pair untouched

    raw = [
        {"name": "奥雷里亚诺", "chapter": 3, "para": 5, "entity": "#2"},  # label -> idx 1
        {"name": "乌尔苏拉", "chapter": 1, "para": 1, "entity": "#3"},    # gone -> dropped
    ]
    out = pipeline._normalize_mentions(raw, data)
    assert [m["entity"] for m in out] == [1]


def test_anthropic_no_response_after_retries():
    """Enrich/review must raise ValueError (not AttributeError) after 3 failed attempts."""
    import webserver.toolbox.curie.api_client as ac

    def fake_post(url, headers, payload, timeout=180):
        raise Exception('HTTP 429')

    def fake_wait(exc, attempt, progress_cb=None, max_retries=3):
        return True  # always retry, exhausting the attempt loop

    old_post, old_wait = ac._http_post, ac._wait_429
    ac._http_post, ac._wait_429 = fake_post, fake_wait
    try:
        prov = ac.AnthropicProvider('k', 'claude-sonnet-4-6')
        base = {'title': 't', 'author': 'a', 'characters': [], 'locations': []}
        calls = [
            (lambda: prov.enrich_book_data(base, 'text'), 'enrich'),
            (lambda: prov.review_spoilers(base, 'mat', {}), 'review'),
        ]
        for fn, label in calls:
            try:
                fn()
                assert False, f'{label} should have raised'
            except ValueError as e:
                assert 'no response from API' in str(e), e
    finally:
        ac._http_post, ac._wait_429 = old_post, old_wait


def test_enrich_mentions_guard_when_list_output():
    """A bare-array response (extract_json succeeds, parsed is a list) must not
    crash the mentions extraction."""
    import webserver.toolbox.curie.api_client as ac

    old_post = ac._http_post

    def fake_post(url, headers, payload, timeout=180):
        return {
            "content": [{"type": "text", "text": "[]"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }, {}

    ac._http_post = fake_post
    try:
        prov = ac.AnthropicProvider('k', 'claude-sonnet-4-6')
        entities, usage, mentions = prov.enrich_book_data(
            {'title': 't', 'author': 'a', 'characters': [], 'locations': []}, 'text')
        assert mentions == []
        assert entities == []  # bare-array response surfaces as-is, no crash
    finally:
        ac._http_post = old_post


def test_to_llm_text_leading_nonblock_dropped():
    """Text before the first block tag must not occupy paragraph slot #1 —
    the injector's _BLOCK_OPEN_RE counter only counts block tags."""
    html = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head><title>X</title></head>\n'
        '<body>\n'
        '<span>preamble</span>\n'
        '<p>正文第一段</p>\n'
        '<p><span>内嵌内容</span>与正文</p>\n'
        '</body>\n'
        '</html>\n'
    ).encode('utf-8')
    paras = epub_utils._to_llm_text(html)
    assert paras[0] == (1, "正文第一段"), paras
    assert paras[1][0] == 2
    assert "内嵌内容" in paras[1][1]  # in-block non-block text is kept


def test_resolve_strips_query():
    assert epub_utils._resolve('OEBPS/', 'ch1.xhtml?ref=1') == 'OEBPS/ch1.xhtml'
    assert epub_utils._resolve('OEBPS/', 'ch1.xhtml#frag') == 'OEBPS/ch1.xhtml'
    assert epub_utils._resolve('OEBPS/', '/ch1.xhtml?x=1') == 'ch1.xhtml'


def test_location_aliases_counted():
    """Locations must count alias occurrences, not just the primary name."""
    chapters = [(1, "老屋在村口。"), (2, "老屋年久失修。")]
    data = {"characters": [],
            "locations": [{"name": "布恩迪亚宅", "aliases": ["老屋"], "description": "x"}]}
    pipeline._add_chapter_and_occurrences(data, chapters)
    loc = data["locations"][0]
    assert loc["chapter"] == 1
    assert loc["occurrences"] == 2


def test_find_ambiguous_names_includes_aliases():
    """Location aliases shared with character names are flagged ambiguous."""
    data = {
        "characters": [{"full_name": "阿尔卡蒂奥", "nicknames": [], "role": "x"}],
        "locations": [{"name": "马孔多镇", "aliases": ["阿尔卡蒂奥"], "type": "x"}],
    }
    amb = pipeline._find_ambiguous_names(data)
    assert "阿尔卡蒂奥" in amb


def test_openai_compat_provider_full_pass():
    """OpenAICompatProvider must not crash on self.max_tokens (missing before)
    in enrich/review, and must route mentions/entities correctly."""
    import json
    import webserver.toolbox.curie.api_client as ac

    old_post = ac._http_post
    reply = {
        "choices": [{"message": {"content": json.dumps({
            "entities": {"title": "t", "author": "a",
                          "characters": [{"full_name": "X"}], "locations": []},
            "mentions": [{"name": "X", "chapter": 1, "para": 2, "entity": "#1"}],
        })}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    def fake_post(url, headers, payload, timeout=180):
        return reply, {}

    ac._http_post = fake_post
    try:
        prov = ac.OpenAICompatProvider('k', 'deepseek-chat', 'https://api.deepseek.com')
        assert prov.max_tokens == 16000
        base = {'title': 't', 'author': 'a', 'characters': [], 'locations': []}
        entities, usage, mentions = prov.enrich_book_data(base, 'text')
        assert entities["characters"][0]["full_name"] == "X"
        assert mentions[0]["entity"] == "#1"
        review = prov.review_spoilers(base, 'mat', {})
        assert review["entities"]["characters"][0]["full_name"] == "X"
        # chat/completions URL construction
        assert prov.api_url == 'https://api.deepseek.com/v1/chat/completions'
    finally:
        ac._http_post = old_post


def test_to_llm_text_nested_block_in_leading():
    """Block tags nested inside leading non-block wrappers (e.g. <header><p>)
    must still occupy their paragraph slots — both sides count block tags."""
    html = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head><title>X</title></head>\n'
        '<body>\n'
        '<header><p>第一章 标题</p></header>\n'
        '<p>正文内容。</p>\n'
        '<p>第二段。</p>\n'
        '</body>\n'
        '</html>\n'
    ).encode('utf-8')
    paras = epub_utils._to_llm_text(html)
    assert [n for n, _ in paras] == [1, 2, 3], paras
    assert paras[0] == (1, "第一章 标题")
    assert paras[1] == (2, "正文内容。")
    # header's own text (if any) before the nested <p> is dropped
    html2 = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<body>\n'
        '<span>游离文本</span><p>首段</p>\n'
        '</body>\n'
        '</html>\n'
    ).encode('utf-8')
    paras2 = epub_utils._to_llm_text(html2)
    assert paras2[0] == (1, "首段"), paras2


def test_inject_no_nested_links_substring_nickname():
    """A short name that is a substring of a longer linked name must not
    create nested <a><a> (e.g. 奥雷里亚诺 inside 奥雷里亚诺上校)."""
    import re
    ch = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head><title>C</title></head>\n'
        '<body>\n'
        '<p>奥雷里亚诺上校走进房间，看着地图。奥雷里亚诺上校沉默了很久，'
        '直到门外的士兵第三次敲门，他才缓缓开口，下令全军撤退，'
        '随后又陷入了长久的沉思，仿佛这场战争从未发生过一样。</p>\n'
        '<p>这里只出现奥雷里亚诺一次，其余段落都保持安静，'
        '没有任何其他名字出现，也不会有多余的角色登场。</p>\n'
        '</body>\n'
        '</html>\n'
    )
    data = {
        "characters": [
            {"full_name": "奥雷里亚诺·布恩迪亚",
             "nicknames": ["奥雷里亚诺", "奥雷里亚诺上校"],
             "role": "protagonist", "description": "上校。", "chapter": 0},
        ],
        "locations": [],
    }
    opf1 = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:test</dc:identifier>
    <dc:title>T</dc:title>
  </metadata>
  <manifest>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
  </spine>
</package>
"""
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                        compress_type=zipfile.ZIP_STORED)
            zf.writestr("META-INF/container.xml", CONTAINER)
            zf.writestr("OEBPS/content.opf", opf1)
            zf.writestr("OEBPS/c1.xhtml", ch)

        stats = epub_injector.inject_footnotes(epub, data, hint_density="every_mention")
        assert stats["refs_injected"] == 3, stats  # 2x 上校 + 1x 短名
        html = read_zip(epub, "OEBPS/c1.xhtml")
        assert not re.search(r"<a\b[^>]*><a\b", html), "nested <a><a> found"
        # short name on its own is still linked
        assert html.count("curie-char-1-v1.xhtml#curie-char-1-v1") == 3


def test_deepseek_url_autofix():
    """Bare DeepSeek hosts get /anthropic appended automatically."""
    from webserver.toolbox.curie.api_client import AnthropicProvider

    p1 = AnthropicProvider('k', 'deepseek-v4-flash', 'https://api.deepseek.com')
    assert p1.api_url == 'https://api.deepseek.com/anthropic/v1/messages'
    assert p1._is_deepseek
    assert p1.max_tokens == 64000
    p2 = AnthropicProvider('k', 'deepseek-v4-flash', 'https://api.deepseek.com/anthropic')
    assert p2.api_url == 'https://api.deepseek.com/anthropic/v1/messages'
    p3 = AnthropicProvider('k', 'claude-sonnet-4-6', 'https://api.anthropic.com')
    assert p3.api_url == 'https://api.anthropic.com/v1/messages'
    assert not p3._is_deepseek
    assert p3.max_tokens == 16000


def test_spoiler_material_spreads_across_chapters():
    """Late-chapter windows are spread evenly so story-end spoilers are covered."""
    import re

    entity = {"full_name": "Alice", "nicknames": [], "chapter": 1}
    chapters = [(n, "Alice did something dramatic. " * 50) for n in range(2, 8)]
    material = pipeline._build_spoiler_material(entity, chapters)
    chs = re.findall(r"\[ch(\d+)\]", material)
    assert len(chs) == 12
    counts = {}
    for c in chs:
        counts[c] = counts.get(c, 0) + 1
    assert set(counts) == {"2", "3", "4", "5", "6", "7"}
    assert set(counts.values()) == {2}

    # more late chapters than windows -> every chapter still gets >= 1
    chapters2 = [(n, "Bob appeared here. " * 50) for n in range(2, 20)]
    m2 = pipeline._build_spoiler_material(
        {"full_name": "Bob", "nicknames": [], "chapter": 1}, chapters2)
    chs2 = re.findall(r"\[ch(\d+)\]", m2)
    assert len(chs2) == 12
    assert "19" in chs2  # very last chapter is covered


def test_rate_limit_detection():
    """429 detection must not over-match '4290' / port numbers."""
    import webserver.toolbox.curie.api_client as ac

    assert ac._is_rate_limit(Exception('HTTP 429: Too Many Requests'))
    assert ac._is_rate_limit(Exception('error code 429'))
    assert not ac._is_rate_limit(Exception('port 4291 refused'))
    assert not ac._is_rate_limit(Exception('error code 4290'))
    assert not ac._is_rate_limit(Exception('timeout'))

    class _FakeResp:
        status_code = 429

    class _HttpErr(Exception):
        def __init__(self):
            self.response = _FakeResp()

    assert ac._is_rate_limit(_HttpErr())


def test_openai_reasoner_max_tokens():
    """deepseek-reasoner gets the same 64k output headroom as Anthropic+DeepSeek."""
    from webserver.toolbox.curie.api_client import OpenAICompatProvider

    assert OpenAICompatProvider('k', 'deepseek-chat').max_tokens == 16000
    assert OpenAICompatProvider('k', 'deepseek-reasoner').max_tokens == 64000
    assert OpenAICompatProvider('k', 'MY-reasoner-X').max_tokens == 64000


def test_strip_injections_keeps_foreign_links():
    """Links pointing at non-curie hrefs (e.g. curie-chapter-1.xhtml) are
    preserved; only our noterefs are stripped (by class, legacy by href)."""
    html = (
        '<p>'
        '<a href="curie-chapter-1.xhtml#c1" epub:type="noteref">章节</a> '
        '<a href="curie-char-1-v1.xhtml#curie-char-1-v1" class="curie-noteref" '
        'epub:type="noteref" role="doc-noteref">老链接</a>'
        '</p>'
    )
    out = epub_injector._strip_injections(html)
    assert 'curie-chapter-1.xhtml' in out          # foreign link untouched
    assert 'curie-char-1-v1.xhtml' not in out      # curie noteref stripped
    assert '老链接' in out                          # inner text kept
    # legacy href-only links (no class) are still removed
    legacy = '<a href="curie-loc-3-v1.xhtml#curie-loc-3-v1" epub:type="noteref">地名</a>'
    assert 'curie-loc-3-v1' not in epub_injector._strip_injections(legacy)


def test_make_link_escapes_bare_lt():
    """Bare '<' in matched text is escaped; entities are left as-is."""
    link = epub_injector._make_link('A < B &amp; C', 'x.xhtml#x',
                                    'epub:type="noteref"')
    assert 'A &lt; B &amp; C' in link
    assert 'class="curie-noteref"' in link


def test_curie_title_suffix_helpers():
    """导读版标题后缀：v1 无序号、v2+ 递增；剥离旧后缀得根名。"""
    from webserver.toolbox.curie.pipeline import curie_suffix_for, strip_curie_suffix

    assert curie_suffix_for(1) == "（Curie 导读版）"
    assert curie_suffix_for(2) == "（Curie 导读版 v2）"
    assert curie_suffix_for(9) == "（Curie 导读版 v9）"
    assert strip_curie_suffix("百年孤独（Curie 导读版）") == "百年孤独"
    assert strip_curie_suffix("百年孤独（Curie 导读版 v3）") == "百年孤独"
    assert strip_curie_suffix("百年孤独") == "百年孤独"
    assert strip_curie_suffix("") == ""


def test_pipeline_cancel_propagates():
    """should_cancel=True 时流水线立即抛 RuntimeError('cancelled')。"""
    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        build_sample_epub(epub)
        out = os.path.join(tmp, "book_data.json")
        try:
            pipeline.run_curie_analysis(
                None, "T", "A", epub, True, True, "English", out,
                should_cancel=lambda: True,
            )
            assert False, "should have raised"
        except RuntimeError as e:
            assert str(e) == "cancelled", e


def test_pipeline_cancel_between_chunks():
    """取消在 enrich 分块之间生效（第二块前触发）。"""
    calls = {"n": 0}

    class _CancelProvider(_MockProvider):
        def enrich_book_data(self, book_data, epub_text, ambiguous_names=None,
                             progress_cb=None, chunk_label=""):
            calls["n"] += 1
            return book_data, {"input_tokens": 1, "output_tokens": 1}, []

    with tempfile.TemporaryDirectory() as tmp:
        epub = os.path.join(tmp, "t.epub")
        build_sample_epub(epub)
        out = os.path.join(tmp, "book_data.json")
        try:
            pipeline.run_curie_analysis(
                _CancelProvider(), "T", "A", epub, True, True, "English", out,
                progress_cb=lambda m: None,
                should_cancel=lambda: calls["n"] >= 1,
            )
            assert False, "should have raised"
        except RuntimeError as e:
            assert str(e) == "cancelled", e


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
