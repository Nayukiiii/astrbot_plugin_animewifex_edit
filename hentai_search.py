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
# 繁简字符映射表（模块级常量，避免每次调用重建）
# 覆盖本子/同人标题中常见的繁体字、旧字体、异体字
# ---------------------------------------------------------------------------
_TRAD_TO_SIMP = str.maketrans(
    "記來術數廣歡戀變覺體學習藝歷傳說話夢劍鬥殺獸魔龍獵師戰艦艷漢澤緒彌綾繪紗縣諸賀總聲顯憂憐戲劇齊憶愛",
    "记来术数广欢恋变觉体学习艺历传说话梦剑斗杀兽魔龙猎师战舰艳汉泽绪弥绫绘纱县诸贺总声显忧怜戏剧齐忆爱",
)

# ---------------------------------------------------------------------------
# 核心搜索器（无框架依赖）
# ---------------------------------------------------------------------------

class HentaiSearcher:
    """
    双站并发搜索器：JM / NH
    可在任意 asyncio 环境中独立使用。
    """

    def __init__(self, config: dict, en_cache_fn=None, en_cache_write_fn=None):
        self.jm_base            = config.get("jm_base_url",  "https://18comic.vip").rstrip("/")
        self.nh_base            = config.get("nh_base_url",  "https://nhentai.net").rstrip("/")
        self.nvidia_api_key     = config.get("nvidia_api_key", "")
        self.nvidia_model       = config.get("nvidia_model",   "meta/llama-3.3-70b-instruct")
        self._en_cache_fn       = en_cache_fn
        self._en_cache_write_fn = en_cache_write_fn

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def search(self, char: str, source: str = "") -> "SearchResult":
        """
        主入口：角色名+作品名 → 多语言多站搜索。

        流程：
          阶段一：JM中文搜索 与 LLM翻译 并发
          阶段二：JM标签页补搜 + NH 并发
          pick_best 用完整关键词库评分选最优 ID
        """
        display = f"《{source}》{char}" if source else char
        char    = char.strip()
        source  = source.strip()

        VTUBER_AGENCIES = {
            "ホロライブ", "hololive", "にじさんじ", "nijisanji",
            "vspo", "ぶいすぽ", "774inc", "のりプロ", "あおぎり高校",
        }
        source_is_agency = source.lower() in VTUBER_AGENCIES
        combined = f"{source} {char}".strip() if source and not source_is_agency else ""

        # en_cache 查询
        en_name_cached = ""
        if self._en_cache_fn:
            try:
                en_name_cached = self._en_cache_fn(char, source) or ""
            except Exception:
                pass

        # 阶段一：JM中文 + LLM 并发
        jm_items_zh, trans = await asyncio.gather(
            self._query_merged(self._search_jm,
                               [combined] if combined else [],
                               [char]),
            self._ai_translate_multi(char, source),
        )

        # 解析 LLM 结果
        en_name   = en_name_cached or (trans.get("en_char")     or "").strip()
        en_source = (trans.get("en_source")    or "").strip()
        short_src = (trans.get("short_source") or "").strip()
        ja_char   = (trans.get("ja_char")      or "").strip()
        kana_char = (trans.get("kana_char")    or "").strip()
        alt_chars = [a.strip() for a in (trans.get("alt_char") or []) if a.strip()]
        is_vtuber = bool(trans.get("is_vtuber"))
        if is_vtuber:
            short_src = ""

        en_combined = (
            f"{en_name} {en_source}".strip()
            if en_name and en_source and not is_vtuber else en_name
        )

        nh_q_en  = list(dict.fromkeys(filter(None, [en_name, en_combined])))
        nh_q_raw = list(dict.fromkeys(filter(None, [char, combined])))

        logger.info(
            f"[查本子] char={char!r} source={source!r} "
            f"en={en_name!r} en_source={en_source!r} | "
            f"JM中文已完成({len(jm_items_zh)}条)"
        )

        # JM 补搜：kana/ja
        jm_extra_q = list(dict.fromkeys(filter(None, [kana_char, ja_char])))
        if jm_extra_q:
            jm_extra = await self._query_merged(self._search_jm, jm_extra_q, [])
            seen_jm  = {iid for iid, _ in jm_items_zh}
            jm_items_base = jm_items_zh + [
                (iid, t) for iid, t in jm_extra if iid not in seen_jm
            ]
        else:
            jm_items_base = jm_items_zh

        # 阶段二：JM标签页 + NH 并发
        _jm_from_tag = False
        _nh_from_tag = False

        async def _jm_pipeline() -> list:
            nonlocal _jm_from_tag
            for tag_q in filter(None, [kana_char]):  # JM是中文站，只用假名，不用英文名
                tag_items = await self._search_jm_tag(tag_q)
                if tag_items:
                    _jm_from_tag = True
                    # 标签页结果 + 普通搜索结果合并，候选池更大
                    seen   = {iid for iid, _ in tag_items}
                    merged = tag_items + [
                        (iid, t) for iid, t in jm_items_base if iid not in seen
                    ]
                    return merged
            return jm_items_base

        async def _nh_pipeline() -> list:
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
            return await self._query_merged(self._search_nh, nh_q_en, nh_q_raw)

        jm_items, nh_items = await asyncio.gather(
            _jm_pipeline(),
            _nh_pipeline(),
        )

        # 回写 en_cache
        if en_name and not en_name_cached and self._en_cache_write_fn:
            try:
                self._en_cache_write_fn(char, source, en_name, alt_chars)
            except Exception:
                pass

        # 构建关键词库
        all_keywords = list(dict.fromkeys(filter(None, [
            char, source,
            en_name, en_source, short_src,
            ja_char, kana_char, en_combined,
        ] + alt_chars)))

        logger.info(f"[查本子] jm({len(jm_items)}) nh({len(nh_items)})")
        logger.info(f"[查本子] keywords={all_keywords}")
        logger.info(f"[查本子] JM前5={[(i, t[:30]) for i, t in jm_items[:5]]}")
        logger.info(f"[查本子] NH前5={[(i, t[:30]) for i, t in nh_items[:5]]}")

        # pick_best：评分优先，score全0时有结果就随机兜底
        jm_id = self._pick_best(jm_items, all_keywords)
        if jm_id is None and jm_items:
            jm_id = random.choice(jm_items)[0]

        nh_id = self._pick_best(nh_items, all_keywords)
        if nh_id is None and nh_items:
            nh_id = random.choice(nh_items)[0]

        logger.info(f"[查本子] 最终 jm_id={jm_id} nh_id={nh_id}")

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
    # 内部：搜索辅助
    # ------------------------------------------------------------------

    @staticmethod
    async def _query_site(search_fn, queries: list) -> list:
        """全部查询都跑完，结果合并去重，候选池最大化。"""
        seen, merged = set(), []
        for q in queries:
            items = await search_fn(q)
            for iid, title in items:
                if iid not in seen:
                    seen.add(iid)
                    merged.append((iid, title))
        return merged

    @staticmethod
    async def _query_merged(search_fn, queries_a: list, queries_b: list) -> list:
        """两路并发，结果合并去重，让 pick_best 在完整候选池里评分。"""
        tasks = []
        if queries_a:
            tasks.append(HentaiSearcher._query_site(search_fn, queries_a))
        if queries_b:
            tasks.append(HentaiSearcher._query_site(search_fn, queries_b))
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

    # ------------------------------------------------------------------
    # 内部：各站搜索
    # ------------------------------------------------------------------

    async def _search_jm(self, q: str) -> list:
        """搜索 18Comic，使用 jmcomic 库绕过 Cloudflare。"""
        def _sync():
            import jmcomic
            import logging as _lg
            _lg.getLogger("jmcomic").setLevel(_lg.ERROR)
            client = jmcomic.JmOption.default().new_jm_client()
            return list(client.search_site(search_query=q))[:30]

        try:
            result = await asyncio.to_thread(_sync)
            logger.info(f"[查本子] JM搜索 {q!r} -> {len(result)} 条")
            return result
        except Exception as e:
            logger.error(f"[查本子] JM搜索失败: {e}")
            return []

    async def _search_jm_tag(self, q: str) -> list:
        """JM 角色标签页（main_tag=3），失败时退回普通搜索。"""
        def _sync():
            import jmcomic
            import logging as _lg
            _lg.getLogger("jmcomic").setLevel(_lg.ERROR)
            client = jmcomic.JmOption.default().new_jm_client()
            try:
                items = list(client.search_site(search_query=q, main_tag=3))[:50]
                if items:
                    return items
            except Exception:
                pass
            try:
                return list(client.search_site(search_query=q))[:30]
            except Exception:
                return []

        try:
            result = await asyncio.to_thread(_sync)
            logger.info(f"[查本子] JM标签页 {q!r} -> {len(result)} 条")
            return result or []
        except Exception as e:
            logger.error(f"[查本子] JM标签页失败: {e}")
            return []

    async def _search_nh_tag(self, en_char: str) -> list:
        """NHentai 角色标签页。标签页403时自动fallback到关键词搜索页。"""
        try:
            import html as html_lib
            from curl_cffi.requests import AsyncSession
            slug     = re.sub(r'\s+', '-', en_char.strip().lower())
            page_num = random.randint(1, 5)
            url      = f"{self.nh_base}/tag/character:{slug}/?sort=popular&page={page_num}"
            async with AsyncSession() as s:
                r = await s.get(
                    url, impersonate="chrome124",
                    headers={"Accept-Language": "en-US,en;q=0.9"},
                    timeout=20,
                )
            if r.status_code == 404:
                logger.info(f"[查本子] NH标签不存在: character:{slug}")
                return []
            if r.status_code != 200:
                # 标签页被封，fallback 到关键词搜索
                logger.warning(f"[查本子] NH标签页{r.status_code}，fallback到搜索页: {en_char!r}")
                return await self._search_nh(en_char)
            page = r.text
            pairs = self._parse_nh_page(page, html_lib)
            logger.info(f"[查本子] NH标签页 character:{slug} -> {len(pairs)} 条")
            return pairs[:30]
        except Exception as e:
            logger.error(f"[查本子] NH标签页失败: {e}")
            return []


    async def _search_nh(self, q: str) -> list:
        """NHentai 关键词搜索，curl_cffi 绕过 Cloudflare 403。"""
        try:
            import html as html_lib
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as s:
                r = await s.get(
                    f"{self.nh_base}/search/?q={quote(q)}",
                    impersonate="chrome124",
                    headers={"Accept-Language": "en-US,en;q=0.9"},
                    timeout=20,
                )
                if r.status_code != 200:
                    logger.warning(f"[查本子] NH搜索状态码: {r.status_code}")
                    return []
                page = r.text
            pairs = self._parse_nh_page(page, html_lib)
            logger.info(f"[查本子] NH搜索 {q!r} -> {len(pairs)} 条")
            return pairs[:30]
        except Exception as e:
            logger.error(f"[查本子] NH搜索失败: {e}")
            return []

    @staticmethod
    def _parse_nh_page(page: str, html_lib) -> list:
        """从 NHentai HTML 提取 (id, title)，三级备用策略。"""
        pairs_raw = re.findall(
            r'href="/g/(\d+)/[^"]*"[^>]*>.*?<div class="caption">(.*?)</div>',
            page, re.S
        )
        if not pairs_raw:
            pairs_raw = re.findall(
                r'href="/g/(\d+)/[^"]*"[^>]*title="([^"]+)"', page
            )
        if not pairs_raw:
            _ids    = re.findall(r'href="/g/(\d+)/"', page)
            _titles = re.findall(
                r'class=["\']title["\'][^>]*>(.*?)</(?:div|p)>', page, re.S
            )
            pairs_raw = list(zip(_ids, _titles))
        if not pairs_raw:
            return []
        return [
            (iid, html_lib.unescape(re.sub(r'<[^>]+>', '', t).strip()))
            for iid, t in pairs_raw if t.strip()
        ]

    # ------------------------------------------------------------------
    # 内部：AI 翻译
    # ------------------------------------------------------------------

    async def _ai_translate_multi(self, char: str, source: str = "") -> dict:
        """调用 NVIDIA NIM LLM 取多语言信息。无 Key 时直接返回空 dict。"""
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
            "- en_source: official English title used by the franchise itself. "
            "MUST NOT be a literal translation. '' if unknown.\n"
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
                        "model":       self.nvidia_model,
                        "messages":    [
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": subject},
                        ],
                        "temperature": 0.0,
                        "max_tokens":  400,
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
        """打码字符→空格，繁体/旧字体→简体。"""
        title = re.sub(r'[●○＊*×]', ' ', title)
        return title.translate(_TRAD_TO_SIMP)

    @staticmethod
    def _kw_match(keyword: str, title_normalized: str) -> bool:
        """
        关键词匹配，支持打码通配：
        - ≤2字符：整词边界匹配
        - 普通：直接包含
        - 含打码：切片各段都在标题里
        """
        kw = keyword.lower()
        t  = title_normalized.lower()
        if len(kw) <= 2:
            return bool(re.search(
                r'(?<![a-z0-9])' + re.escape(kw) + r'(?![a-z0-9])', t
            ))
        if kw in t:
            return True
        parts = [p for p in re.split(r'[\s●○＊*×]+', kw) if len(p) >= 2]
        return bool(parts) and all(p in t for p in parts)

    @classmethod
    def _pick_best(cls, items: list, keywords: list) -> str | None:
        """
        从候选列表选最相关条目，返回 ID。
        score 全 0 时返回 None（宁缺毋滥）。
        同分时随机取一条。
        """
        if not items:
            return None
        kws = [k.lower() for k in keywords if k]
        if not kws:
            return None

        def score(title: str) -> int:
            normalized = cls._normalize_title(title)
            return sum(1 for k in kws if cls._kw_match(k, normalized))

        scored = [(score(title), iid) for iid, title in items]
        max_s  = max(s for s, _ in scored)
        if max_s == 0:
            return None
        return random.choice([iid for s, iid in scored if s == max_s])


# ---------------------------------------------------------------------------
# 搜索结果数据类
# ---------------------------------------------------------------------------

class SearchResult:
    """封装双站搜索结果，提供格式化输出。"""

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
    def _fmt_title(title: str, limit: int = 20) -> str:
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
            line("JM", self.jm_id, self.jm_title),
            line("NH", self.nh_id, self.nh_title),
        ]
        header = (
            f"未找到「{self.display}」的相关本子～"
            if self.all_not_found
            else f"以下结果以「{self.display}」搜索，仅供参考，不保证相关性："
        )
        return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# AstrBot 事件处理 Mixin
# ---------------------------------------------------------------------------

class HentaiSearchHandler:
    """备用 AstrBot Mixin，main.py 已直接使用 HentaiSearcher。"""

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
# 独立测试入口
# ---------------------------------------------------------------------------

async def _demo():
    config = {
        "jm_base_url":    os.getenv("JM_BASE_URL",    "https://18comic.vip"),
        "nh_base_url":    os.getenv("NH_BASE_URL",    "https://nhentai.net"),
        "nvidia_api_key": os.getenv("NVIDIA_API_KEY", ""),
        "nvidia_model":   os.getenv("NVIDIA_MODEL",   "meta/llama-3.3-70b-instruct"),
    }
    searcher = HentaiSearcher(config)
    result   = await searcher.search(char="平泽唯", source="轻音少女")
    print(result.format_text())


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_demo())
