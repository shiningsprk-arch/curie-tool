"""
Curie — MyBooks toolbox tool.

Ported from Fank1/curie (https://github.com/Fank1/curie, a Calibre plugin by
Fank1) with the author's explicit permission. Core logic lives in the
`curie/` subpackage; this class adapts it to the MyBooks Toolbox pattern
(BaseTool + background task + encrypted config + re-import as a new book).
"""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import traceback
from typing import Optional

from webserver.i18n import _
from webserver.services import AsyncService
from webserver.services.background_service import BackgroundService, BackgroundTask
from webserver.toolbox.base_tool import BaseTool
from webserver.toolbox.curie import pipeline
from webserver.toolbox.curie.api_client import make_provider, test_connection
from webserver.toolbox.curie.pipeline import load_book_data

logger = logging.getLogger("curie_tool")

PBKDF2_ITER = 100000
CONFIG_FILE = "api_config.enc"
KEY_FILE = ".curie_key"
MAX_TITLE_LEN = 120

# "重新入库" 时新书的标题后缀（v1 无序号，后续版本递增序号，全部保留）
TITLE_SUFFIX = "（Curie 导读版）"


# 语言代码 → 提示语言名（跟随书籍元数据时使用）
LANGUAGE_CODE_MAP = {
    "zh": "Chinese", "zh-cn": "Chinese", "zho": "Chinese",
    "zh-tw": "Traditional Chinese", "zh-hant": "Traditional Chinese",
    "zh-hk": "Traditional Chinese",
    "en": "English", "eng": "English", "en-us": "English", "en-gb": "English",
    "sv": "Svenska", "swe": "Svenska", "sv-se": "Svenska",
    "ja": "Japanese", "jpn": "Japanese",
    "fr": "French", "fra": "French",
    "de": "German", "deu": "German",
    "es": "Spanish", "spa": "Spanish",
    "ko": "Korean", "kor": "Korean",
}
FALLBACK_LANGUAGE = "Chinese"

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_COMPAT = "openai_compat"

DENSITY_OPTIONS = ("every_mention", "every_10_paragraphs", "once_per_chapter")
# 上游 curie 的默认档位；语义 = 每章首次出现必标一次，之后每 10 段一次
DEFAULT_DENSITY = "every_10_paragraphs"


class CurieTool(BaseTool):
    service_item_name = "Curie 无剧透导读"

    _curie_lock = threading.Lock()
    _last_task_id: Optional[int] = None

    @classmethod
    def is_running(cls) -> bool:
        task = cls.get_last_task()
        return bool(task and task.get("status") == BackgroundTask.STATUS_RUNNING)

    @classmethod
    def get_last_task(cls) -> Optional[dict]:
        if cls._last_task_id is None:
            return None
        return BackgroundService().get_task(cls._last_task_id)

    @staticmethod
    def _task_cancelled(task_id: Optional[int]) -> bool:
        """任务是否已被标记为取消（BackgroundService.cancel_task 只改状态，
        运行线程通过轮询此状态来实现可取消）。"""
        if task_id is None:
            return False
        task = BackgroundService().get_task(task_id)
        return bool(task and task.get("status") == BackgroundTask.STATUS_CANCELLED)

    @staticmethod
    def info() -> dict:
        return {
            "tool_id": "curie",
            "name": "Curie 无剧透导读",
            "description": "基于 Fank1/curie（已获作者许可）移植：用 LLM（Claude / DeepSeek 等）生成书籍角色与地点的无剧透简介，"
                           "以 EPUB 3 弹窗脚注形式注入新 EPUB 并重新入库，原文件零改动。",
            "revision": "0.1.0",
            "author": "shiningsprk-arch",
            "publish_date": "2026-08-07",
        }

    # ── 语言解析 ──────────────────────────────────────────────────────────

    @staticmethod
    def resolve_language(languages, override: str = "auto") -> str:
        """优先用页面覆盖值；否则跟随书籍元数据语言；都没有则回退中文。"""
        if override and override != "auto":
            return override
        if languages:
            code = str(languages[0]).strip().lower()
            if code in LANGUAGE_CODE_MAP:
                return LANGUAGE_CODE_MAP[code]
        return FALLBACK_LANGUAGE

    # ── 加密配置（仿 mimo_tts 的持久化方案） ──────────────────────────────

    def _config_dir(self) -> str:
        return self.get_work_dir("")

    def _key_path(self) -> str:
        return os.path.join(self._config_dir(), KEY_FILE)

    def _config_path(self) -> str:
        return os.path.join(self._config_dir(), CONFIG_FILE)

    def _ensure_key(self) -> bytes:
        path = self._key_path()
        if os.path.exists(path) and os.path.getsize(path) == 32:
            with open(path, "rb") as f:
                return f.read()
        key = os.urandom(32)
        os.makedirs(self._config_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(key)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
        return key

    @staticmethod
    def _derive_key(master: bytes, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", master, salt, PBKDF2_ITER, dklen=32)

    def _encrypt(self, data: bytes) -> str:
        master = self._ensure_key()
        salt = os.urandom(16)
        key = self._derive_key(master, salt)
        iv = os.urandom(16)
        keystream = b""
        pos = 0
        while len(keystream) < len(data):
            keystream += hashlib.sha256(key + iv + pos.to_bytes(4, "big")).digest()
            pos += 1
        cipher = bytes(a ^ b for a, b in zip(data, keystream[:len(data)]))
        return base64.b64encode(salt + iv + cipher).decode()

    def _decrypt(self, token: str) -> Optional[bytes]:
        master = self._ensure_key()
        raw = base64.b64decode(token)
        if len(raw) < 32:
            return None
        salt, iv, cipher = raw[:16], raw[16:32], raw[32:]
        key = self._derive_key(master, salt)
        keystream = b""
        pos = 0
        while len(keystream) < len(cipher):
            keystream += hashlib.sha256(key + iv + pos.to_bytes(4, "big")).digest()
            pos += 1
        return bytes(a ^ b for a, b in zip(cipher, keystream[:len(cipher)]))

    def save_api_config(self, config: dict) -> None:
        token = self._encrypt(json.dumps(config).encode())
        with open(self._config_path(), "w") as f:
            f.write(token)

    def load_api_config(self) -> Optional[dict]:
        path = self._config_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                token = f.read().strip()
            decrypted = self._decrypt(token)
            if decrypted is None:
                return None
            return json.loads(decrypted.decode())
        except Exception:
            return None

    def clear_api_config(self) -> None:
        path = self._config_path()
        if os.path.exists(path):
            os.remove(path)

    # ── 测试连接 ───────────────────────────────────────────────────────────

    def test_connection(self, provider: str, api_key: str, model: str,
                        api_url: str = "") -> tuple[bool, str]:
        try:
            reply = test_connection(provider, api_key, model, api_url)
            self.save_api_config({
                "provider": provider,
                "api_key": api_key,
                "api_url": api_url,
                "model": model,
            })
            return True, reply
        except Exception as e:
            return False, str(e)

    # ── Curie 版本书目标题（v1 / v2 / v3 … 全部保留） ─────────────────────

    def _title_exists(self, title: str) -> bool:
        """库中是否已存在该书名的书。优先精确搜索，失败时遍历元数据。"""
        try:
            safe = title.replace('"', "")
            return bool(self.db.new_api.search('title:"=%s"' % safe))
        except Exception as err:
            logger.warning("[CurieTool] title search failed, fallback to scan: %s", err)
        try:
            for bid in self.get_all_book_ids():
                mi = self.get_book_metadata(bid)
                if (mi.title or "").strip() == title:
                    return True
        except Exception as err:
            logger.warning("[CurieTool] title scan failed: %s", err)
        return False

    def next_curie_title(self, book_title: str) -> str:
        """为 Curie 导读版生成不冲突的书名：v1 无后缀，冲突则 v2、v3… 递增。"""
        root = pipeline.strip_curie_suffix(book_title)
        for version in range(1, 1000):
            suffix = pipeline.curie_suffix_for(version)
            candidate = root[:MAX_TITLE_LEN - len(suffix)] + suffix
            if not self._title_exists(candidate):
                return candidate
        raise RuntimeError(_("Curie 导读版标题序号已用尽，请整理书库"))

    # ── 书籍数据处理（同步查询） ──────────────────────────────────────────

    @AsyncService.register_function
    def preview(self, book_id: int) -> dict:
        """返回已生成的导读数据（角色/地点 JSON），未生成则返回 None。"""
        data_path = os.path.join(self.get_work_dir(str(book_id)), "book_data.json")
        data = load_book_data(data_path)
        if not data:
            return {"data": None}
        return {"data": {
            "title": data.get("title", ""),
            "author": data.get("author", ""),
            "language": data.get("language", ""),
            "characters": data.get("characters", []),
            "locations": data.get("locations", []),
        }}

    # ── 主流程 ─────────────────────────────────────────────────────────────

    @AsyncService.register_service
    def convert(self, book_id: int, user_id: int, provider: str,
                api_key: str, model: str, api_url: str = "",
                include_characters: bool = True, include_places: bool = False,
                language: str = "auto", hint_density: str = DEFAULT_DENSITY) -> None:
        """完整流程：分析 → 注入新 EPUB → 重新入库。"""
        if not CurieTool._curie_lock.acquire(blocking=False):
            logger.warning("[CurieTool] Already running, skipping convert for book_id=%d", book_id)
            return

        task_id = None
        error_message = None
        book_title = "Unknown"

        try:
            # create_task must live inside the try: if it raises (DB failure),
            # the finally block still releases the lock and no task id is
            # reported (otherwise every later run would silently no-op).
            task_id = self.create_task(progress_data={"status": "starting", "book_id": book_id})
            CurieTool._last_task_id = task_id

            def _progress(percent: int, stage: str, **extra):
                data = {"status": "running", "stage": stage, "book_id": book_id}
                data.update(extra)
                self.update_task_progress(task_id, min(percent, 99), data)

            def _check_cancel():
                if CurieTool._task_cancelled(task_id):
                    raise RuntimeError("cancelled")

            books = self.db.get_data_as_dict(ids=[book_id])
            if not books:
                raise RuntimeError(_("书籍不存在：ID=%d") % book_id)
            book = books[0]
            book_title = book.get("title", "Unknown")
            fmts = [f.upper() for f in (book.get("available_formats") or [])]
            if "EPUB" not in fmts:
                raise RuntimeError(_("该书籍没有 EPUB 格式，无法生成导读"))
            epub_path = self.db.format_abspath(book_id, "EPUB", index_is_id=True)
            if not epub_path or not os.path.exists(epub_path):
                raise RuntimeError(_("找不到 EPUB 文件"))

            mi = self.get_book_metadata(book_id)
            authors = [a for a in (list(mi.authors) if mi.authors else [])]
            raw_langs = list(mi.languages) if getattr(mi, "languages", None) else []
            hint_lang = self.resolve_language(raw_langs, language)

            if hint_density not in DENSITY_OPTIONS:
                hint_density = "every_10_paragraphs"

            _progress(3, "prepare")

            work_dir = self.get_work_dir(str(book_id))
            source_copy = os.path.join(work_dir, "source.epub")
            shutil.copy2(epub_path, source_copy)

            data_path = os.path.join(work_dir, "book_data.json")
            output_epub = os.path.join(work_dir, "curie_out.epub")

            _progress(5, "step1")
            prov = make_provider(provider, api_key, model, api_url)

            # 文本进度 → 百分比：Step1 5-45, Step2 45-90, Step3 90-99
            # 节流：429 等待期间 progress_cb 每秒触发，相同 (stage, pct) 不重复写库
            _last_progress = [None]

            def _update(pct: int, stage: str):
                if _last_progress[0] == (pct, stage):
                    return
                _last_progress[0] = (pct, stage)
                self.update_task_progress(task_id, pct, {
                    "status": "running", "stage": stage, "book_id": book_id,
                })

            def _stage_progress(msg: str):
                # 取消检查放在最前：429 等待期间 progress_cb 每秒触发，可立即中断
                _check_cancel()
                msg_l = msg.lower()
                if "step 1" in msg_l:
                    _update(15, "step1")
                elif "rate limit" in msg_l:
                    _update(60, "step2")
                elif "step 2" in msg_l and "counting" in msg_l:
                    _update(55, "step2")
                elif "step 2" in msg_l and "parsing" in msg_l:
                    _update(50, "step2")
                elif "step 2" in msg_l and "part" in msg_l:
                    m = re.search(r'part (\d+)/(\d+)', msg)
                    if m:
                        p, t = int(m.group(1)), int(m.group(2))
                        pct = 55 + int((p / max(t, 1)) * 33) if t > 1 else 88
                        _update(min(pct, 90), "step2")
                    else:
                        _update(88, "step2")
                elif "step 3" in msg_l:
                    _update(92, "step3")
                elif "saving" in msg_l:
                    _update(90, "step2")
                else:
                    _update(88, "step2")

            book_data = pipeline.run_curie_analysis(
                prov, book_title, ", ".join(authors) or "Unknown", source_copy,
                include_characters, include_places, hint_lang, data_path,
                progress_cb=_stage_progress,
                should_cancel=lambda: CurieTool._task_cancelled(task_id),
            )
            if not book_data.get("characters") and not book_data.get("locations"):
                raise RuntimeError(_("未从本书识别到任何角色或地点，已停止（未生成导读）"))
            book_data["language"] = hint_lang

            # 记录语言到 JSON（与 preview 一致）
            with open(data_path, "w", encoding="utf-8") as f:
                json.dump(book_data, f, indent=2, ensure_ascii=False)

            _progress(93, "inject")
            pipeline.build_curied_epub(source_copy, output_epub, book_data, hint_density)
            _check_cancel()

            # ── 重新入库（新书） ─────────────────────────────────────────
            _progress(96, "import")
            new_title = self.next_curie_title(book_title)
            new_book_id = self.import_file(
                user_id, output_epub, new_title, authors, delete_after_import=False,
            )
            if new_book_id is None:
                raise RuntimeError(_("重新入库失败：Calibre 未返回书籍 ID"))

            self.update_task_progress(task_id, 100, {
                "status": "completed",
                "book_id": book_id,
                "new_book_id": new_book_id,
                "new_title": new_title,
                "language": hint_lang,
                "characters": len(book_data.get("characters", [])),
                "locations": len(book_data.get("locations", [])),
            })

            self.add_msg(user_id, "success",
                         _("《%s》的 Curie 导读已生成：%d 个角色、%d 个地点，新书《%s》已入库（原文件未改动）")
                         % (book_title, len(book_data.get("characters", [])),
                            len(book_data.get("locations", [])), new_title))

        except Exception as err:
            if str(err) == "cancelled" or CurieTool._task_cancelled(task_id):
                logger.info("[CurieTool] Convert cancelled for book_id=%d", book_id)
                self.add_msg(user_id, "info", _("《%s》的 Curie 导读生成已取消") % book_title)
            else:
                error_message = str(err)
                self.add_msg(user_id, "danger", _("《%s》的 Curie 导读生成失败：%s") % (book_title, str(err)))
                logger.error("[CurieTool] Convert failed for book_id=%d: %s", book_id, err)
                logger.error(traceback.format_exc())
        finally:
            # 用户取消时保留 cancelled 状态（complete_task 会把它覆盖回 completed）
            if task_id is not None and not CurieTool._task_cancelled(task_id):
                self.complete_task(task_id, error_message=error_message)
            CurieTool._curie_lock.release()

    @AsyncService.register_service
    def regenerate(self, book_id: int, user_id: int,
                   hint_density: str = DEFAULT_DENSITY) -> None:
        """用已缓存的 book_data.json 重新注入（改密度），不消耗 API 额度。"""
        if not CurieTool._curie_lock.acquire(blocking=False):
            logger.warning("[CurieTool] Already running, skipping regenerate for book_id=%d", book_id)
            return

        task_id = None
        error_message = None
        book_title = "Unknown"

        try:
            task_id = self.create_task(progress_data={"status": "starting", "book_id": book_id})
            CurieTool._last_task_id = task_id

            def _check_cancel():
                if CurieTool._task_cancelled(task_id):
                    raise RuntimeError("cancelled")

            books = self.db.get_data_as_dict(ids=[book_id])
            if not books:
                raise RuntimeError(_("书籍不存在：ID=%d") % book_id)
            book = books[0]
            book_title = book.get("title", "Unknown")
            fmts = [f.upper() for f in (book.get("available_formats") or [])]
            if "EPUB" not in fmts:
                raise RuntimeError(_("该书籍没有 EPUB 格式，无法生成导读"))
            epub_path = self.db.format_abspath(book_id, "EPUB", index_is_id=True)
            if not epub_path or not os.path.exists(epub_path):
                raise RuntimeError(_("找不到 EPUB 文件"))

            work_dir = self.get_work_dir(str(book_id))
            data_path = os.path.join(work_dir, "book_data.json")
            book_data = load_book_data(data_path)
            if not book_data:
                raise RuntimeError(_("尚未生成过导读数据，请先运行一次生成任务"))

            if hint_density not in DENSITY_OPTIONS:
                hint_density = "every_10_paragraphs"

            self.update_task_progress(task_id, 20, {"status": "running", "stage": "inject", "book_id": book_id})
            source_copy = os.path.join(work_dir, "source.epub")
            if not os.path.exists(source_copy):
                shutil.copy2(epub_path, source_copy)
            output_epub = os.path.join(work_dir, "curie_out.epub")

            pipeline.build_curied_epub(source_copy, output_epub, book_data, hint_density)
            _check_cancel()

            self.update_task_progress(task_id, 70, {"status": "running", "stage": "import", "book_id": book_id})
            mi = self.get_book_metadata(book_id)
            authors = [a for a in (list(mi.authors) if mi.authors else [])]
            new_title = self.next_curie_title(book_title)
            new_book_id = self.import_file(
                user_id, output_epub, new_title, authors, delete_after_import=False,
            )
            if new_book_id is None:
                raise RuntimeError(_("重新入库失败：Calibre 未返回书籍 ID"))

            self.update_task_progress(task_id, 100, {
                "status": "completed",
                "book_id": book_id,
                "new_book_id": new_book_id,
                "new_title": new_title,
            })
            self.add_msg(user_id, "success",
                         _("《%s》的 Curie 导读已按新密度重新生成，新书《%s》已入库")
                         % (book_title, new_title))

        except Exception as err:
            if str(err) == "cancelled" or CurieTool._task_cancelled(task_id):
                logger.info("[CurieTool] Regenerate cancelled for book_id=%d", book_id)
                self.add_msg(user_id, "info", _("《%s》的 Curie 导读重新生成已取消") % book_title)
            else:
                error_message = str(err)
                self.add_msg(user_id, "danger", _("《%s》的 Curie 导读重新生成失败：%s") % (book_title, str(err)))
                logger.error("[CurieTool] Regenerate failed for book_id=%d: %s", book_id, err)
                logger.error(traceback.format_exc())
        finally:
            if task_id is not None and not CurieTool._task_cancelled(task_id):
                self.complete_task(task_id, error_message=error_message)
            CurieTool._curie_lock.release()
