from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class CharacterResolver:
    """Resolve user-entered character names into review candidates."""

    def __init__(self, user_agent: str = "astrbot_plugin_animewifex/1.0"):
        self.user_agent = user_agent

    async def search_female_characters(self, name: str, limit: int = 5, source: str = "") -> list[dict]:
        """Search Bangumi, AniList, then VNDB, returning normalized candidates."""

        def _src_match(candidate_src: str, filter_src: str) -> bool:
            a = candidate_src.strip().lower()
            b = filter_src.strip().lower()
            return bool(a and b and (b in a or a in b))

        seen: set = set()
        results: list = []

        def _add(r: dict):
            key = (r.get("name", "").strip().lower(), r.get("source", "").strip().lower())
            if key[0] and key not in seen:
                seen.add(key)
                results.append(r)

        bgm_results = await self.search_bangumi(name, limit * 3)
        ja_names_from_bgm: list[str] = []
        for r in bgm_results:
            if source and not _src_match(r.get("source", ""), source):
                continue
            _add(r)
            ja = r["name"]
            if ja and ja != name and ja not in ja_names_from_bgm:
                ja_names_from_bgm.append(ja)

        for ja in ja_names_from_bgm[:2]:
            if len(results) >= limit:
                break
            for r in await self.search_anilist(ja, limit):
                if source and not _src_match(r.get("source", ""), source):
                    continue
                _add(r)

        if len(results) < limit:
            for r in await self.search_anilist(name, limit):
                if source and not _src_match(r.get("source", ""), source):
                    continue
                _add(r)

        if len(results) < limit:
            for r in await self.search_vndb_characters(name, limit, source=source):
                if source and not _src_match(r.get("source", ""), source):
                    continue
                _add(r)

        if not results:
            for r in bgm_results:
                _add(r)
            if len(results) < limit:
                for r in await self.search_anilist(name, limit):
                    _add(r)
            if len(results) < limit:
                for r in await self.search_vndb_characters(name, limit, source=""):
                    _add(r)

        return results[:limit]

    async def search_bangumi(self, name: str, limit: int) -> list[dict]:
        try:
            url = "https://api.bgm.tv/v0/search/characters"
            headers = {"User-Agent": self.user_agent, "Content-Type": "application/json"}
            body = {"keyword": name, "filter": {}}
            params = {"limit": limit * 3, "offset": 0}
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=body, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        logger.warning("[添老婆][Bangumi] HTTP %d", r.status)
                        return []
                    data = await r.json()
        except Exception as e:
            logger.error("[添老婆] Bangumi 搜索失败: %s", e)
            return []

        items = data.get("data") or []
        logger.info(
            "[添老婆][Bangumi] 搜索 %r 返回%d条: %s",
            name, len(items), [(x.get("name"), x.get("gender")) for x in items[:10]],
        )
        results = []
        for item in items:
            if item.get("gender") == "male":
                continue
            char_name = item.get("name", "")
            if not char_name:
                continue
            char_source = ""
            for info in item.get("infobox", []):
                key = info.get("key", "")
                if key in ("登场作品", "组合", "出处", "所属作品", "来源作品"):
                    val = info.get("value", "")
                    if isinstance(val, list):
                        val = val[0].get("v", "") if val else ""
                    char_source = str(val).strip()
                    if char_source:
                        break
            images = item.get("images", {})
            thumb = images.get("small") or images.get("medium") or ""
            if thumb and not thumb.startswith("http"):
                thumb = "https:" + thumb
            results.append({"name": char_name, "source": char_source, "thumb_url": thumb})
            if len(results) >= limit:
                break
        logger.info("[添老婆][Bangumi] 过滤后: %s", [r["name"] for r in results])
        return results

    async def search_anilist(self, name: str, limit: int) -> list[dict]:
        query = """
query ($search: String) {
  Page(page: 1, perPage: 20) {
    characters(search: $search) {
      name { full native }
      gender
      image { medium }
      media(perPage: 1) {
        nodes { title { native romaji english } }
      }
    }
  }
}
"""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://graphql.anilist.co",
                    json={"query": query, "variables": {"search": name}},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        body = await r.text()
                        logger.warning("[添老婆][AniList] HTTP %d body=%s", r.status, body[:300])
                        return []
                    data = await r.json()
        except Exception as e:
            logger.error("[添老婆] AniList 搜索失败: %s", e)
            return []

        chars = data.get("data", {}).get("Page", {}).get("characters", [])
        logger.info(
            "[添老婆][AniList] 搜索 %r 返回%d条: %s",
            name, len(chars), [(c.get("name", {}).get("full"), c.get("gender")) for c in chars[:10]],
        )

        def _is_japanese(s):
            return any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in s)

        def _is_korean(s):
            return any("\uac00" <= ch <= "\ud7a3" for ch in s)

        results = []
        for c in chars:
            gender = (c.get("gender") or "").lower()
            if gender == "male":
                continue
            native = c["name"].get("native") or ""
            full = c["name"].get("full") or ""
            if native and _is_japanese(native):
                char_name = native
            elif full and not _is_korean(full):
                char_name = full
            elif native:
                char_name = native
            else:
                char_name = full
            if not char_name:
                continue
            media_nodes = (c.get("media") or {}).get("nodes", [])
            source = ""
            if media_nodes:
                t = media_nodes[0].get("title", {})
                source = t.get("native") or t.get("romaji") or t.get("english") or ""
            thumb = (c.get("image") or {}).get("medium") or ""
            results.append({"name": char_name, "source": source, "thumb_url": thumb})
            if len(results) >= limit:
                break
        logger.info("[添老婆][AniList] 过滤后: %s", [r["name"] for r in results])
        return results

    async def search_vndb_characters(self, name: str, limit: int, source: str = "") -> list[dict]:
        results = []
        try:
            payload = {
                "filters": ["search", "=", name],
                "fields": "name,original,image.url,vns.title,vns.alttitle",
                "sort": "searchrank",
                "results": min(limit * 3, 25),
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.vndb.org/kana/character",
                    json=payload,
                    headers={"Content-Type": "application/json", "User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as r:
                    if r.status != 200:
                        logger.warning("[添老婆][VNDB搜索] HTTP %d name=%r", r.status, name)
                        return []
                    data = await r.json()

            source_low = source.strip().lower()
            seen = set()
            for item in data.get("results") or []:
                char_name = (item.get("original") or item.get("name") or "").strip()
                if not char_name:
                    continue
                titles = []
                for vn in item.get("vns") or []:
                    if vn.get("title"):
                        titles.append(vn.get("title"))
                    if vn.get("alttitle"):
                        titles.append(vn.get("alttitle"))
                char_source = next((t for t in titles if t), "")
                haystack = " ".join(titles).lower()
                if source_low and source_low not in haystack and not any(
                    source_low in t.lower() or t.lower() in source_low for t in titles
                ):
                    continue
                key = (char_name, char_source)
                if key in seen:
                    continue
                seen.add(key)
                thumb = ((item.get("image") or {}).get("url") or "").strip()
                results.append({"name": char_name, "source": char_source, "thumb_url": thumb})
                if len(results) >= limit:
                    break
        except Exception as e:
            logger.error("[添老婆] VNDB 角色搜索失败: %s", e)
            return []

        logger.info("[添老婆][VNDB搜索] 过滤后: %s", [r["name"] for r in results])
        return results
