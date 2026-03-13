"""
hentai_search.py — 查本子功能独立模块
======================================
从 astrbot_plugin_animewifex 剥离，可单独使用或作为独立插件集成。

对外暴露：
  - HentaiSearcher          核心搜索器类（无框架依赖，可单独测试）
  - HentaiSearchHandler     AstrBot 事件处理 Mixin（需要 AstrBot 环境）

配置项（对应 _conf_schema.json）：
  jm_base_url     18Comic 域名，默认 https://18comic.vip
  nh_base_url     NHentai 域名，默认 https://nhentai.net
  cf_proxy_url    Cloudflare Worker 代理 URL（JM/DL/EH 共用）
  nvidia_api_key  NVIDIA NIM API Key（用于 AI 多语言翻译）
  nvidia_model    翻译模型名，默认 meta/llama-3.3-70b-instruct
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 核心搜索器（无框架依赖）
# ---------------------------------------------------------------------------

class HentaiSearcher:
    """
    四站并发搜索器：JM / NH / EH / DL
    可在任意 asyncio 环境中独立使用。

    使用示例：
        searcher = HentaiSearcher(config={...})
        result = await searcher.search(char="桐人", source="刀剑神域")
        print(result.format_text())
    """

    def __init__(self, config: dict, en_cache_fn=None):
        self.jm_base    = config.get("jm_base_url",  "https://18comic.vip").rstrip("/")
        self.nh_base    = config.get("nh_base_url",  "https://nhentai.net").rstrip("/")
        self.nvidia_api_key = config.get("nvidia_api_key", "")
        self.nvidia_model   = config.get("nvidia_model",   "meta/llama-3.3-70b-instruct")
        # 回调：(char, source) -> str | None，返回角色英文名，查不到返回 None
        # 由 main.py 注入，避免模块间循环依赖
        self._en_cache_fn = en_cache_fn

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def search(self, char: str, source: str = "") -> "SearchResult":
        """
        主入口：根据角色名+作品名进行多语言多站搜索。

        执行流程（两阶段）：
          阶段一：JM（中文词有效）与 LLM 翻译并发
                  LLM 用于获取 en_source / en_char / alt_char，补全搜索词
          阶段二：LLM 结果出来后，NH / EH / DL 用英文词并发搜
          pick_best 用完整 keywords 从候选池选最相关 ID

        en_cache 命中时 en_name 已知，LLM 仍跑（取 en_source），
        总延迟 ≈ max(JM延迟, LLM延迟)，NH/EH/DL 在阶段二再并发。
        """
        display = f"《{source}》{char}" if source else char
        char   = char.strip()
        source = source.strip()

        VTUBER_AGENCIES = {
            "ホロライブ", "hololive", "にじさんじ", "nijisanji",
            "vspo", "ぶいすぽ", "774inc", "のりプロ", "あおぎり高校",
        }
        source_is_agency = source.lower() in VTUBER_AGENCIES
        combined = f"{source} {char}".strip() if source and not source_is_agency else ""

        # ── 查 en_cache ──────────────────────────────────────────────
        en_name_cached = ""
        if self._en_cache_fn:
            try:
                en_name_cached = self._en_cache_fn(char, source) or ""
            except Exception:
                pass

        async def query_site(search_fn, queries: list) -> list:
            """轮询：依次尝试每个 query，拿到结果就返回"""
            for q in queries:
                items = await search_fn(q)
                if items:
                    return items
            return []

        async def query_merged(search_fn, queries_a: list, queries_b: list) -> list:
            """两路并发搜索，结果合并去重。
            两路都强制搜（不再因为英文路有结果就跳过中文路），
            让 pick_best 在完整候选池里用关键词评分选最佳。
            """
            tasks = []
            if queries_a:
                tasks.append(query_site(search_fn, queries_a))
            if queries_b:
                # 中文路也强制搜，不依赖英文路是否有结果
                tasks.append(query_site(search_fn, queries_b))
            if not tasks:
                return []
            results = await asyncio.gather(*tasks)
            seen, merged = set(), []
            for items in results:
                for iid, title in items:
                    if iid not in seen:
                        seen.add(iid)
                        merged.append((iid, title))
            return merged

        # ── 阶段一：JM 与 LLM 翻译并发 ──────────────────────────────
        # JM 搜中文词有效，不依赖 LLM 结果，可以和翻译同时跑
        # LLM 目的：取 en_source / en_char / short_source / alt_char / kana_char
        jm_items_zh, trans = await asyncio.gather(
            query_merged(self._search_jm, [combined] if combined else [], [char]),
            self._ai_translate_multi(char, source),
        )

        # ── 解析 LLM 结果 ────────────────────────────────────────────
        en_name   = en_name_cached or (trans.get("en_char") or "").strip()
        en_source = (trans.get("en_source")    or "").strip()
        short_src = (trans.get("short_source") or "").strip()
        ja_char   = (trans.get("ja_char")      or "").strip()
        alt_chars = [a.strip() for a in (trans.get("alt_char") or []) if a.strip()]
        is_vtuber = bool(trans.get("is_vtuber"))
        if is_vtuber:
            short_src = ""

        # "Anzai Chiyomi Girls und Panzer" —— 能直接命中 NH/EH 标题
        en_combined = (
            f"{en_name} {en_source}".strip()
            if en_name and en_source and not is_vtuber else en_name
        )

        # NH 搜索词：英文+中文并行
        nh_q_en  = list(dict.fromkeys(filter(None, [en_name, en_combined])))
        nh_q_raw = list(dict.fromkeys(filter(None, [char, combined])))

        kana_char = (trans.get("kana_char") or "").strip()

        logger.info(
            f"[查本子] char={char!r} source={source!r} "
            f"en={en_name!r} en_source={en_source!r} short={short_src!r} | "
            f"JM已完成({len(jm_items_zh)}条) NH={nh_q_en or nh_q_raw}"
        )

        # ── JM 补搜：用 kana_char/ja_char 补一次，合并去重 ──────────
        jm_extra_q = list(dict.fromkeys(filter(None, [kana_char, ja_char])))
        if jm_extra_q:
            jm_extra = await query_merged(self._search_jm, jm_extra_q, [])
            seen_jm  = {iid for iid, _ in jm_items_zh}
            jm_items = jm_items_zh + [(iid, t) for iid, t in jm_extra if iid not in seen_jm]
        else:
            jm_items = jm_items_zh

        _nh_from_tag = False
        _jm_from_tag = False

        async def _nh_with_tag_fallback() -> list:
            nonlocal _nh_from_tag
            if en_name:
                tag_items = await self._search_nh_tag(en_name)
                if tag_items:
                    _nh_from_tag = True
                    return tag_items
                for alt in alt_chars:
                    tag_items = await self._search_nh_tag(alt)
                    if tag_items:
                        _nh_from_tag = True
                        return tag_items
            return await query_merged(self._search_nh, nh_q_en, nh_q_raw)

        async def _jm_with_tag_fallback() -> list:
            """JM 标签页：用 kana_char 或 en_name 走标签页，全量返回供随机取"""
            nonlocal _jm_from_tag
            for tag_q in filter(None, [kana_char, en_name] + alt_chars):
                tag_items = await self._search_jm_tag(tag_q)
                if tag_items:
                    _jm_from_tag = True
                    return tag_items
            return jm_items

        # ── 阶段二：JM标签页 / NH 并发 ──────────────────────────────
        nh_items, jm_items = await asyncio.gather(
            _nh_with_tag_fallback(),
            _jm_with_tag_fallback(),
        )

        # ── 回写 en_cache ────────────────────────────────────────────
        if en_name and not en_name_cached:
            try:
                write_fn = getattr(self, "_en_cache_write_fn", None)
                if write_fn:
                    write_fn(char, source, en_name, alt_chars)
            except Exception:
                pass

        # ── keywords + pick_best ─────────────────────────────────────
        all_keywords = list(dict.fromkeys(filter(None, [
            char, source,
            en_name, en_source, short_src,
            ja_char, kana_char, en_combined,
        ] + alt_chars)))

        logger.info(f"[查本子] jm({len(jm_items)}) nh({len(nh_items)})")
        logger.info(f"[查本子] keywords={all_keywords}")
        logger.info(f"[查本子] JM={[(i,t[:25]) for i,t in jm_items[:5]]}")
        logger.info(f"[查本子] NH={[(i,t[:25]) for i,t in nh_items[:5]]}")

        jm_id = random.choice(jm_items)[0] if (jm_items and _jm_from_tag) else self._pick_best(jm_items, all_keywords)
        nh_id = random.choice(nh_items)[0] if (nh_items and _nh_from_tag) else self._pick_best(nh_items, all_keywords)

        logger.info(f"[查本子] jm_id={jm_id} nh_id={nh_id}")

        jm_title = next((t for rid, t in jm_items if rid == jm_id), "") if jm_id else ""
        nh_title = next((t for rid, t in nh_items if rid == nh_id), "") if nh_id else ""

        return SearchResult(
            display=display,
            jm_id=jm_id, jm_title=jm_title,
            nh_id=nh_id, nh_title=nh_title,
            jm_base=self.jm_base,
            nh_base=self.nh_base,
        )

        # ------------------------------------------------------------------
    # 内部：各站搜索
    # ------------------------------------------------------------------

    async def _search_jm(self, q: str) -> list:
        """
        搜索 18Comic（禁漫天堂）。
        使用 jmcomic 库的 API client，绕过 Cloudflare 网页验证。
        """
        def _sync_search():
            import jmcomic
            # 关闭 stderr 日志噪音
            import logging as _logging
            _logging.getLogger("jmcomic").setLevel(_logging.ERROR)
            option = jmcomic.JmOption.default()
            client = option.new_jm_client()
            res = client.search_site(search_query=q)
            return list(res)[:30]

        try:
            result = await asyncio.to_thread(_sync_search)
            logger.info(f"[查本子] JM搜索 {q!r} -> {len(result)} 条")
            return result  # 已经是 (album_id, title) tuple 列表
        except Exception as e:
            logger.error(f"[查本子] JM搜索失败: {e}")
            return []

    async def _search_jm_tag(self, q: str) -> list:
        """
        JM 标签页搜索：用角色名（日文假名/英文）走 JM 的女角色标签。
        返回该标签下所有本子，供随机取一条。
        """
        def _sync_tag():
            import jmcomic
            import logging as _logging
            _logging.getLogger("jmcomic").setLevel(_logging.ERROR)
            option = jmcomic.JmOption.default()
            client = option.new_jm_client()
            # JM 标签搜索：main_tag=3 为角色标签
            try:
                res = client.search_site(search_query=q, main_tag=3)
                items = list(res)[:50]
                if items:
                    return items
            except Exception:
                pass
            # 退回普通搜索
            try:
                res = client.search_site(search_query=q)
                return list(res)[:30]
            except Exception:
                return []

        try:
            result = await asyncio.to_thread(_sync_tag)
            logger.info(f"[查本子] JM标签页 {q!r} -> {len(result)} 条")
            return result
        except Exception as e:
            logger.error(f"[查本子] JM标签页失败: {e}")
            return []

    async def _search_nh_tag(self, en_char: str) -> list:
        """
        走 NHentai 角色标签页取本子，100% 角色相关。
        en_char 转成 danbooru slug：小写，空格→连字符。
        """
        try:
            import html as html_lib
            slug = re.sub(r'\s+', '-', en_char.strip().lower())
            # 随机选页，增加结果多样性（先抓第1页确认标签存在，再随机页）
            page_num = random.randint(1, 5)
            url  = f"{self.nh_base}/tag/character:{slug}/?sort=popular&page={page_num}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 404:
                        logger.info(f"[查本子] NH标签页不存在: {slug}")
                        return []
                    if r.status != 200:
                        logger.warning(f"[查本子] NH标签页状态码: {r.status}")
                        return []
                    page = await r.text()

            pairs_raw = re.findall(
                r'href="/g/(\d+)/[^"]*"[^>]*>.*?<div class="caption">(.*?)</div>',
                page, re.S
            )
            if not pairs_raw:
                pairs_raw = re.findall(
                    r'href="/g/(\d+)/[^"]*"[^>]*title="([^"]+)"', page
                )
            pairs = [
                (iid, html_lib.unescape(re.sub(r'<[^>]+>', '', t).strip()))
                for iid, t in pairs_raw if t.strip()
            ]
            logger.info(f"[查本子] NH标签页 character:{slug} -> {len(pairs)} 条")
            return pairs[:30]
        except Exception as e:
            logger.error(f"[查本子] NH标签页失败: {e}")
            return []

    async def _search_nh(self, q: str) -> list:
        """
        搜索 NHentai。
        标题从 <div class='caption'> 内的 <div class='title'> 或 <p class='title'> 取，
        不是从 .caption 本身取（.caption 是标签栏，不是标题）。
        """
        try:
            import html as html_lib
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(
                    f"{self.nh_base}/search/?q={quote(q)}",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        logger.warning(f"[查本子] NH搜索状态码: {r.status}")
                        return []
                    page = await r.text()

            # 联合提取 ID + caption（nhentai 搜索结果实际结构）
            pairs_raw = re.findall(
                r'href="/g/(\d+)/[^"]*"[^>]*>.*?<div class="caption">(.*?)</div>',
                page, re.S
            )
            if not pairs_raw:
                # 备用1：href title 属性
                pairs_raw = re.findall(
                    r'href="/g/(\d+)/[^"]*"[^>]*title="([^"]+)"',
                    page
                )
            if not pairs_raw:
                # 备用2：分开提取 id + class=title
                _ids = re.findall(r'href="/g/(\d+)/"', page)
                _titles = re.findall(
                    r'class=["\']+title["\']+[^>]*>(.*?)</(?:div|p)>',
                    page, re.S
                )
                pairs_raw = list(zip(_ids, _titles))

            if not pairs_raw:
                return []

            pairs = [
                (iid, html_lib.unescape(re.sub(r'<[^>]+>', '', t).strip()))
                for iid, t in pairs_raw
                if t.strip()
            ]
            logger.info(f"[查本子] NH搜索 {q!r} -> {len(pairs)} 条")
            return pairs[:30]
        except Exception as e:
            logger.error(f"[查本子] NH搜索失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 内部：AI 翻译
    # ------------------------------------------------------------------

    async def _ai_translate_multi(self, char: str, source: str = "") -> dict:
        """
        调用 NVIDIA NIM LLM，一次性返回角色/作品的多语言信息。
        无 API Key 时立即返回空 dict（不发请求，不阻塞）。
        """
        if not char or not self.nvidia_api_key:
            return {}

        subject = f"角色名：{char}"
        if source:
            subject += f"\n作品名：{source}"

        system_prompt = (
            "You are an expert in Japanese anime, manga, visual novel, galgame, and VTuber culture.\n"
            "Given a CHARACTER NAME and optionally a SERIES TITLE, output a JSON object.\n"
            "\n"
            "STRICT RULES:\n"
            "1. The character MUST belong to the given series. Do NOT substitute a different character.\n"
            "2. If you are not confident about a field, return empty string or empty list. DO NOT GUESS.\n"
            "3. en_char MUST be the name used on booru/doujin sites (Pixiv, Danbooru, Gelbooru).\n"
            "4. Do NOT confuse characters with similar names from other series.\n"
            "5. If the series is a VTuber agency/group (ホロライブ, にじさんじ, VSPO, 774inc, "
            "Hololive, Nijisanji, indie VTuber), set is_vtuber=true and short_source=''.\n"
            "6. For VTubers, en_char should be the romanized talent name as used on Pixiv/Danbooru.\n"
            "\n"
            "Output fields:\n"
            "- zh_char: Chinese name (simplified). '' if unknown.\n"
            "- en_char: English/Romaji name most used on booru/doujin sites. '' if unsure.\n"
            "- ja_char: Japanese kana/kanji name. '' if unknown.\n"
            "- kana_char: Katakana reading. '' if unknown.\n"
            "- alt_char: list of aliases/romanizations used on doujin sites. [] if none.\n"
            "- en_source: official English title used by the franchise itself (e.g. 'BlazBlue' not 'Azure Grimoire'). MUST NOT be a literal translation of the Chinese/Japanese title. '' if unknown or unsure.\n"
            "- ja_source: full Japanese series title. '' if unknown.\n"
            "- short_source: abbreviated series title used on doujin/booru. '' if VTuber or none.\n"
            "- is_vtuber: true if VTuber talent, false otherwise.\n"
            "\n"
            "Output ONLY valid JSON. No markdown, no code blocks, no extra text."
        )

        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.nvidia_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.nvidia_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": subject},
                        ],
                        "temperature": 0.0,
                        "max_tokens": 400,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 200:
                        j    = await r.json()
                        text = j["choices"][0]["message"]["content"].strip()
                        text = re.sub(r'^```(?:json)?\s*', '', text)
                        text = re.sub(r'\s*```$',          '', text)
                        result = json.loads(text)
                        if not isinstance(result.get("alt_char"), list):
                            result["alt_char"] = []
                        return result
                    else:
                        logger.warning(f"[查本子] NV API 状态码: {r.status}")
        except Exception as e:
            logger.warning(f"[查本子] NV翻译失败: {e}")
        return {}

    # ------------------------------------------------------------------
    # 内部：相关性挑选
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_title(title: str) -> str:
        """
        标准化标题：
        1. 打码字符（●○＊*×）替换成空格
        2. 中日高频异体字统一为简体（本子标题常见繁体/旧字体）
        """
        title = re.sub(r'[●○＊*×]', ' ', title)
        VARIANTS = str.maketrans(
            "記來術數廣歡戀變覺體學習藝歷傳說話夢劍鬥殺獸魔龍獵師戰艦艷漢",
            "记来术数广欢恋变觉体学习艺历传说话梦剑斗杀兽魔龙猎师战舰艳汉",
        )
        return title.translate(VARIANTS)

    @staticmethod
    def _kw_match(keyword: str, title_normalized: str) -> bool:
        """
        关键词匹配，支持打码通配。
        直接包含 → 命中；否则按空格/打码符切片，各片段都在标题里 → 命中。
        """
        kw = keyword.lower()
        t  = title_normalized.lower()
        # 短词（≤2字符，如 "es"）用整词匹配，避免误命中任意含该字母的单词
        if len(kw) <= 2:
            return bool(re.search(r'(?<![a-z0-9])' + re.escape(kw) + r'(?![a-z0-9])', t))
        if kw in t:
            return True
        parts = [p for p in re.split(r'[\s●○＊*×]+', kw) if len(p) >= 2]
        if not parts:
            return False
        return all(p in t for p in parts)

    @classmethod
    def _pick_best(cls, items: list, keywords: list) -> str | None:
        """
        从搜索结果里选最相关的条目，返回 ID。
        所有结果 score=0（完全无关）时返回 None，宁缺毋滥。
        """
        if not items:
            return None
        kws = [k.lower() for k in keywords if k]

        def score(title: str) -> int:
            normalized = cls._normalize_title(title)
            return sum(1 for k in kws if cls._kw_match(k, normalized))

        scored  = [(score(title), iid) for iid, title in items]
        max_s   = max(s for s, _ in scored)
        if max_s == 0:
            return None
        return random.choice([iid for s, iid in scored if s == max_s])


# ---------------------------------------------------------------------------
# 搜索结果数据类
# ---------------------------------------------------------------------------

class SearchResult:
    """封装四站搜索结果，提供格式化输出。"""

    def __init__(
        self,
        display:  str,
        jm_id:    str | None,
        jm_title: str,
        nh_id:    str | None,
        nh_title: str,
        jm_base:  str,
        nh_base:  str,
    ):
        self.display  = display
        self.jm_id    = jm_id
        self.jm_title = jm_title
        self.nh_id    = nh_id
        self.nh_title = nh_title
        self.jm_base  = jm_base
        self.nh_base  = nh_base

    @property
    def all_not_found(self) -> bool:
        return not any([self.jm_id, self.nh_id])

    @staticmethod
    def _fmt_title(title: str, limit: int = 10) -> str:
        """截断标题，超出加省略号"""
        if not title:
            return ""
        return title[:limit] + "…" if len(title) > limit else title

    def format_text(self) -> str:
        def line(label, id_, title):
            if not id_:
                return f"{label}: not found"
            t = self._fmt_title(title)
            return f"{label}: {id_} | {t}" if t else f"{label}: {id_}"

        lines = [
            line("JM",    self.jm_id, self.jm_title),
            line("NH",    self.nh_id, self.nh_title),
        ]

        header = (
            f"未找到「{self.display}」的相关本子～"
            if self.all_not_found
            else f"以下结果以「{self.display}」搜索，仅供参考，不保证相关性："
        )
        return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# AstrBot 事件处理 Mixin（死代码，main.py 已直接调 HentaiSearcher，保留备用）
# ---------------------------------------------------------------------------

class HentaiSearchHandler:
    """
    AstrBot 插件 Mixin，提供「要本子」指令处理逻辑。
    main.py 里 WifePlugin 已直接持有 HentaiSearcher 实例，
    本 Mixin 仅作为备用独立插件场景使用。
    """

    def _get_searcher(self) -> HentaiSearcher:
        if not hasattr(self, "_hentai_searcher"):
            self._hentai_searcher = HentaiSearcher(self.config)
        return self._hentai_searcher

    def _parse_wife_info(self, wife_data: list) -> tuple[str, str]:
        raw = os.path.splitext(wife_data[0])[0].split("/")[-1]
        if "!" in raw:
            source_name, char_name = raw.split("!", 1)
        else:
            source_name, char_name = "", raw
        return source_name, char_name


# ---------------------------------------------------------------------------
# 独立测试入口（不依赖 AstrBot）
# ---------------------------------------------------------------------------

async def _demo():
    """命令行快速测试：python hentai_search.py"""
    config = {
        "jm_base_url":    os.getenv("JM_BASE_URL",    "https://18comic.vip"),
        "nh_base_url":    os.getenv("NH_BASE_URL",    "https://nhentai.net"),
        "nvidia_api_key": os.getenv("NVIDIA_API_KEY", ""),
        "nvidia_model":   os.getenv("NVIDIA_MODEL",   "meta/llama-3.3-70b-instruct"),
    }
    searcher = HentaiSearcher(config)
    result   = await searcher.search(char="我妻由乃", source="未来日記")
    print(result.format_text())


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_demo())