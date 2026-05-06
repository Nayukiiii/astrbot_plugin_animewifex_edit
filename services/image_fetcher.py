from __future__ import annotations

import asyncio
import logging
import random
import re
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


class ImageFetcher:
    """Fetch candidate character images for add-wife review."""

    def __init__(self, config: dict, translate_fn):
        self.config = config
        self.translate_fn = translate_fn
        self.pixiv_refresh_token = config.get("pixiv_refresh_token", "")
        self.user_agent = "astrbot_plugin_animewifex/1.0"

    async def fetch_character_images(
        self, char_name: str, source: str, count: int = 3, fallback_thumb_url: str = ""
    ) -> list[bytes]:
        """Fetch review images from accurate sources first, then broad fallbacks."""
        translations = await self.translate_fn(char=char_name, source=source)
        en_name = translations.get("en_char", char_name)
        ja_name = translations.get("ja_char", char_name)
        kana_name = translations.get("kana_char", "")
        alt_chars = translations.get("alt_char", [])
        en_source = translations.get("en_source", source)
        ja_source = translations.get("ja_source", source)
        short_source = translations.get("short_source", "")

        def to_tag(s: str) -> str:
            return re.sub(r"[\s·・･/\\:]+", "_", s.strip().lower()).strip("_")

        char_tag_sources = [en_name, ja_name] + alt_chars + [char_name]
        booru_char_tags = list(dict.fromkeys(to_tag(s) for s in char_tag_sources if s and s.strip()))

        source_tag_sources = [short_source, en_source, ja_source, source]
        booru_src_tags = list(dict.fromkeys(to_tag(s) for s in source_tag_sources if s and s.strip()))

        booru_combo_tags = []
        for ctag in booru_char_tags:
            for stag in booru_src_tags:
                booru_combo_tags.append(f"{ctag} {stag}")
        booru_combo_tags = list(dict.fromkeys(booru_combo_tags))
        booru_fallback_tags = booru_char_tags

        pixiv_combined = []
        for cname in [ja_name, en_name, char_name]:
            for sname in [ja_source, short_source, source]:
                if cname and sname and cname.strip() and sname.strip():
                    pixiv_combined.append(f"{cname.strip()} {sname.strip()}")
        pixiv_tag_sources = pixiv_combined + [en_name, ja_name] + alt_chars + [char_name]
        pixiv_tags = list(dict.fromkeys(s.strip() for s in pixiv_tag_sources if s and s.strip()))

        source_queries = list(dict.fromkeys(filter(None, [ja_source, short_source, en_source, source])))
        images: list[bytes] = []

        async def fetch_booru_series(tag_queries: list[str], label: str) -> None:
            for source_label, fetcher in (
                ("Gelbooru", self.gelbooru_fetch),
                ("Yande.re", self.yandere_fetch),
                ("Danbooru", self.danbooru_fetch),
                ("Konachan", self.konachan_fetch),
            ):
                if len(images) >= count:
                    break
                for q in tag_queries:
                    if len(images) >= count:
                        break
                    images.extend(await fetcher(q, count - len(images)))
                logger.info("[image-fetch] %s %s total=%d", label, source_label, len(images))

        if self.pixiv_refresh_token and len(images) < count:
            for q in pixiv_tags:
                if len(images) >= count:
                    break
                images.extend(await self.pixiv_fetch(q, count - len(images)))
            logger.info("[image-fetch] Pixiv total=%d", len(images))

        extra_sources_raw = self.config.get("extra_image_sources", "")
        extra_sources = [s.strip() for s in extra_sources_raw.split(",") if s.strip()]
        if extra_sources and len(images) < count:
            for src_name in extra_sources:
                if len(images) >= count:
                    break
                for q in pixiv_tags:
                    if len(images) >= count:
                        break
                    images.extend(await self.custom_source_fetch(src_name, q, count - len(images)))
            logger.info("[image-fetch] custom(%s) total=%d", ",".join(extra_sources), len(images))

        if len(images) < count and self.config.get("shuushuu_access_token"):
            images.extend(await self.shuushuu_fetch(char_name, en_name, kana_name, source, count - len(images)))
            logger.info("[image-fetch] shuushuu total=%d", len(images))

        if len(images) < count and booru_combo_tags:
            await fetch_booru_series(booru_combo_tags, "combo-tags")

        if len(images) < count:
            for q in list(dict.fromkeys([char_name, ja_name, kana_name, en_name] + alt_chars)):
                if len(images) >= count:
                    break
                if not q:
                    continue
                imgs = await self.vndb_fetch(q, source, count - len(images))
                if imgs:
                    images.extend(imgs)
                    logger.info("[image-fetch] VNDB total=%d", len(images))
                    break

        if len(images) < count and booru_fallback_tags:
            await fetch_booru_series(booru_fallback_tags, "char-tags")

        if not images:
            for q in source_queries:
                imgs = await self.getchu_fetch(q, count)
                if imgs:
                    images.extend(imgs)
                    logger.info("[image-fetch] Getchu total=%d", len(images))
                    break

        if not images:
            for q in source_queries:
                imgs = await self.dlsite_cover_fetch(q, count)
                if imgs:
                    images.extend(imgs)
                    logger.info("[image-fetch] DLsite total=%d", len(images))
                    break

        if not images and fallback_thumb_url:
            thumb = await self.download_image_url(fallback_thumb_url, min_size=2 * 1024)
            if thumb:
                images.append(thumb)

        return images[:count]

    async def download_image_url(self, url: str, min_size: int = 10 * 1024) -> bytes | None:
        if not url:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url,
                    headers={"User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status == 200:
                        data = await r.read()
                        if len(data) >= min_size:
                            return data
        except Exception as e:
            logger.warning("[image-fetch] image download failed: %s", e)
        return None

    async def shuushuu_fetch(
        self, char_name: str, en_name: str, kana_name: str, source: str, count: int
    ) -> list[bytes]:
        images = []
        access = self.config.get("shuushuu_access_token", "")
        refresh = self.config.get("shuushuu_refresh_token", "")
        if not access:
            return []

        headers_base = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Cookie": f"access_token={access}; refresh_token={refresh}",
            "Referer": "https://e-shuushuu.net/",
        }
        try:
            async with aiohttp.ClientSession() as s:
                vndb_en = ""
                for sname in list(dict.fromkeys(filter(None, [kana_name, char_name]))):
                    vndb_en = await self.vndb_lookup_en(s, sname, source)
                    if vndb_en:
                        break
                char_query = vndb_en or en_name
                if not char_query:
                    return []

                char_tag_id = await self.shuushuu_find_tag(s, headers_base, char_query, source, tag_type=4)
                if not char_tag_id:
                    return []
                source_tag_id = None
                if source:
                    source_tag_id = await self.shuushuu_find_tag(s, headers_base, source, "", tag_type=None)
                images = await self.shuushuu_fetch_images(s, headers_base, char_tag_id, source_tag_id, count)
        except Exception as e:
            logger.error("[image-fetch] shuushuu failed: %s", e)
        return images

    async def vndb_lookup_en(self, session: aiohttp.ClientSession, char_name: str, source: str) -> str:
        try:
            payload = {
                "filters": ["search", "=", char_name],
                "fields": "id,name,original,vns.title,vns.alttitle",
                "results": 20,
            }
            async with session.post(
                "https://api.vndb.org/kana/character",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return ""
                data = await r.json()
            source_low = source.strip().lower()
            scored = []
            for c in data.get("results", []):
                name = c.get("name", "")
                original = c.get("original") or ""
                vn_titles = " ".join(
                    (v.get("title") or "") + " " + (v.get("alttitle") or "")
                    for v in c.get("vns", [])
                ).lower()
                score = 0
                if original == char_name:
                    score += 10
                if source_low and source_low in vn_titles:
                    score += 15
                scored.append((score, name))
            scored.sort(reverse=True)
            if scored and scored[0][0] > 0:
                return scored[0][1]
        except Exception as e:
            logger.warning("[image-fetch] VNDB lookup failed: %s", e)
        return ""

    async def shuushuu_find_tag(
        self, session: aiohttp.ClientSession, headers: dict, query: str, source: str, tag_type: int | None
    ) -> int | None:
        try:
            url = f"https://e-shuushuu.net/api/v1/tags/?search={quote(query)}&limit=20"
            async with session.get(
                url,
                headers={**headers, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
            tags = data.get("tags", [])
            query_low = query.strip().lower()
            source_low = source.strip().lower()

            for t in tags:
                if tag_type is not None and t.get("type") != tag_type:
                    continue
                if t["title"].lower() == query_low:
                    return t["tag_id"]

            if source_low:
                for t in tags:
                    if tag_type is not None and t.get("type") != tag_type:
                        continue
                    haystack = (t["title"] + " " + (t.get("desc") or "")).lower()
                    if source_low in haystack:
                        return t["tag_id"]

            filtered = [t for t in tags if tag_type is None or t.get("type") == tag_type]
            if filtered:
                best = max(filtered, key=lambda t: t.get("usage_count", 0))
                return best["tag_id"]
        except Exception as e:
            logger.warning("[image-fetch] shuushuu tag failed: %s", e)
        return None

    async def shuushuu_fetch_images(
        self, session: aiohttp.ClientSession, headers: dict,
        char_tag_id: int, source_tag_id: int | None, count: int,
    ) -> list[bytes]:
        images = []
        try:
            search_tag_id = source_tag_id if source_tag_id else char_tag_id
            url = f"https://e-shuushuu.net/api/v1/images/?tags={search_tag_id}&limit={min(count * 6, 40)}"
            async with session.get(
                url,
                headers={**headers, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()

            all_images = data.get("images", [])
            if source_tag_id:
                all_images = [
                    img for img in all_images
                    if any(
                        (t.get("tag_id") if isinstance(t, dict) else t) == char_tag_id
                        for t in (img.get("tags") or [])
                    )
                ]
            random.shuffle(all_images)
            async with aiohttp.ClientSession() as dl:
                for img in all_images:
                    if len(images) >= count:
                        break
                    fn = img.get("filename", "")
                    ext = img.get("ext", "jpg")
                    if not fn:
                        continue
                    img_url = f"https://e-shuushuu.net/images/{fn}.{ext}"
                    data = await self._download_with_session(dl, img_url, headers={"User-Agent": headers["User-Agent"]})
                    if data and len(data) > 10 * 1024:
                        images.append(data)
        except Exception as e:
            logger.error("[image-fetch] shuushuu images failed: %s", e)
        return images

    async def gelbooru_fetch(self, query: str, count: int) -> list[bytes]:
        images = []
        try:
            params = {
                "page": "dapi", "s": "post", "q": "index", "json": 1,
                "tags": f"{query} rating:general",
                "limit": min(count * 4, 40),
            }
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://gelbooru.com/index.php",
                    params=params,
                    headers={"User-Agent": self.user_agent},
                    cookies={"fringeBenefits": "yep"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()
                posts = data.get("post") or []
                random.shuffle(posts)
                for post in posts:
                    if len(images) >= count:
                        break
                    url = post.get("sample_url") or post.get("file_url")
                    if self._valid_static_image_url(url):
                        data = await self._download_with_session(s, url)
                        if data:
                            images.append(data)
        except Exception as e:
            logger.error("[image-fetch] Gelbooru failed: %s", e)
        return images

    async def yandere_fetch(self, query: str, count: int) -> list[bytes]:
        images = []
        try:
            params = {"tags": f"{query} rating:s", "limit": min(count * 4, 40), "page": 1}
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://yande.re/post.json",
                    params=params,
                    headers={"User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        return []
                    posts = await r.json()
                random.shuffle(posts)
                for post in posts:
                    if len(images) >= count:
                        break
                    url = post.get("sample_url") or post.get("jpeg_url") or post.get("file_url")
                    if self._valid_static_image_url(url):
                        data = await self._download_with_session(s, url)
                        if data:
                            images.append(data)
        except Exception as e:
            logger.error("[image-fetch] Yande.re failed: %s", e)
        return images

    async def getchu_fetch(self, source_query: str, count: int) -> list[bytes]:
        images = []
        try:
            from curl_cffi.requests import AsyncSession

            search_url = (
                "https://www.getchu.com/php/search.phtml"
                f"?search_keyword={quote(source_query)}&genre=pc_soft&search=search"
            )
            async with AsyncSession() as s:
                r = await s.get(
                    search_url,
                    impersonate="chrome120",
                    headers={"Accept-Language": "ja,en;q=0.9", "Referer": "https://www.getchu.com/"},
                    timeout=20,
                    allow_redirects=True,
                )
                if r.status_code != 200:
                    return []
                product_ids = re.findall(r"soft\.phtml\?id=(\d+)", r.text)
                if not product_ids:
                    return []
                pid = product_ids[0]
                r2 = await s.get(
                    f"https://www.getchu.com/soft.phtml?id={pid}&gc=gc",
                    impersonate="chrome120",
                    headers={"Accept-Language": "ja,en;q=0.9", "Referer": "https://www.getchu.com/"},
                    timeout=20,
                    allow_redirects=True,
                )
                if r2.status_code != 200:
                    return []
                raw_urls = re.findall(
                    r'["\']((https?:)?//(?:www|img)\.getchu\.com/[^"\']+\.(?:jpg|png))["\']',
                    r2.text,
                    re.I,
                )
            seen: set = set()
            full_urls = []
            for groups in raw_urls:
                u = groups[0]
                full = ("https:" + u) if u.startswith("//") else u
                is_chara = any(k in full.lower() for k in ("bodypaint", "chara", "sample", "brandnew"))
                if full not in seen and is_chara:
                    seen.add(full)
                    full_urls.append(full)
            for groups in raw_urls:
                u = groups[0]
                full = ("https:" + u) if u.startswith("//") else u
                if full not in seen:
                    seen.add(full)
                    full_urls.append(full)
            async with aiohttp.ClientSession() as sess:
                for url in full_urls:
                    if len(images) >= count:
                        break
                    data = await self._download_with_session(sess, url, headers={"Referer": "https://www.getchu.com/"})
                    if data and len(data) > 20 * 1024:
                        images.append(data)
        except Exception as e:
            logger.error("[image-fetch] Getchu failed: %s", e)
        return images

    async def vndb_fetch(self, char_name: str, source: str, count: int) -> list[bytes]:
        images = []
        try:
            payload = {
                "filters": ["search", "=", char_name],
                "fields": "name, image.url, image.sexual, vns.title",
                "sort": "searchrank",
                "results": min(count * 4, 20),
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.vndb.org/kana/character",
                    json=payload,
                    headers={"Content-Type": "application/json", "User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()

                def _score(item: dict) -> int:
                    sc = 0
                    for vn in item.get("vns") or []:
                        if source and source.lower() in (vn.get("title") or "").lower():
                            sc += 10
                            break
                    if (item.get("image") or {}).get("url"):
                        sc += 1
                    return sc

                results = [r for r in data.get("results") or [] if (r.get("image") or {}).get("url")]
                results = [r for r in results if ((r.get("image") or {}).get("sexual") or 0) < 2]
                results.sort(key=_score, reverse=True)
                for item in results:
                    if len(images) >= count:
                        break
                    url = (item.get("image") or {}).get("url")
                    data_bytes = await self._download_with_session(s, url, headers={"User-Agent": self.user_agent})
                    if data_bytes and len(data_bytes) > 5 * 1024:
                        images.append(data_bytes)
        except Exception as e:
            logger.error("[image-fetch] VNDB failed: %s", e)
        return images

    async def dlsite_cover_fetch(self, source_query: str, count: int) -> list[bytes]:
        images = []
        try:
            from curl_cffi.requests import AsyncSession

            async def _get_cover_urls(site: str) -> list[str]:
                url = (
                    f"https://www.dlsite.com/{site}/fsr/=/language/jp"
                    f"/keyword/{quote(source_query)}/order/trend/per_page/5"
                )
                async with AsyncSession() as sess:
                    r = await sess.get(
                        url,
                        impersonate="chrome120",
                        headers={"Accept-Language": "ja,en;q=0.9", "Referer": "https://www.dlsite.com/"},
                        timeout=20,
                        allow_redirects=True,
                    )
                    if r.status_code != 200:
                        return []
                    imgs = re.findall(r'src="(//img\.dlsite\.jp/[^"]+\.jpg)"', r.text)
                    return ["https:" + u for u in imgs[:count * 2]]

            cover_urls = []
            for site in ("maniax", "girls"):
                cover_urls.extend(await _get_cover_urls(site))
            seen: set = set()
            cover_urls = [u for u in cover_urls if not (u in seen or seen.add(u))]  # type: ignore
            async with aiohttp.ClientSession() as s:
                for url in cover_urls:
                    if len(images) >= count:
                        break
                    data = await self._download_with_session(s, url, headers={"Referer": "https://www.dlsite.com/"})
                    if data:
                        images.append(data)
        except Exception as e:
            logger.error("[image-fetch] DLsite failed: %s", e)
        return images

    async def custom_source_fetch(self, source_name: str, query: str, count: int) -> list[bytes]:
        source_name = source_name.strip().lower()
        if source_name == "lolicon":
            return await self.lolicon_fetch(query, count)
        logger.warning("[image-fetch] unknown custom source: %r", source_name)
        return []

    async def lolicon_fetch(self, query: str, count: int) -> list[bytes]:
        images = []
        try:
            params = {"keyword": query, "r18": 0, "num": min(count * 2, 20), "size": ["original", "regular"]}
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.lolicon.app/setu/v2",
                    json=params,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()
                for item in data.get("data") or []:
                    if len(images) >= count:
                        break
                    urls = item.get("urls") or {}
                    url = urls.get("original") or urls.get("regular")
                    data_bytes = await self._download_with_session(s, url, headers={"Referer": "https://www.pixiv.net/"})
                    if data_bytes and len(data_bytes) > 10 * 1024:
                        images.append(data_bytes)
        except Exception as e:
            logger.error("[image-fetch] lolicon failed: %s", e)
        return images

    async def pixiv_fetch(self, query: str, count: int) -> list[bytes]:
        images = []
        try:
            from pixivpy3 import AppPixivAPI

            api = AppPixivAPI()
            await asyncio.get_event_loop().run_in_executor(None, api.auth, None, None, self.pixiv_refresh_token)
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.search_illust(query, search_target="partial_match_for_tags", filter="for_ios"),
            )
            posts = result.illusts or []
            posts = [p for p in posts if getattr(p, "x_restrict", 1) == 0]
            posts.sort(key=lambda p: getattr(p, "total_bookmarks", 0), reverse=True)
            async with aiohttp.ClientSession() as s:
                for post in posts:
                    if len(images) >= count:
                        break
                    url = post.image_urls.get("large") or post.image_urls.get("medium")
                    data = await self._download_with_session(s, url, headers={"Referer": "https://www.pixiv.net/"})
                    if data:
                        images.append(data)
        except ImportError:
            logger.warning("[image-fetch] pixivpy3 is not installed")
        except Exception as e:
            logger.error("[image-fetch] Pixiv failed: %s", e)
        return images

    async def konachan_fetch(self, query: str, count: int) -> list[bytes]:
        images = []
        try:
            params = {"tags": f"{query} rating:safe", "limit": count * 3, "page": 1}
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://konachan.com/post.json",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        return []
                    posts = await r.json()
                random.shuffle(posts)
                for post in posts:
                    if len(images) >= count:
                        break
                    url = post.get("sample_url") or post.get("jpeg_url") or post.get("file_url")
                    data = await self._download_with_session(s, url)
                    if data:
                        images.append(data)
        except Exception as e:
            logger.error("[image-fetch] Konachan failed: %s", e)
        return images

    async def danbooru_fetch(self, query: str, count: int) -> list[bytes]:
        images = []
        try:
            params = {"tags": f"{query} rating:general", "limit": min(count * 3, 20), "page": 1}
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://danbooru.donmai.us/posts.json",
                    params=params,
                    headers={"User-Agent": self.user_agent},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        return []
                    posts = await r.json()
                random.shuffle(posts)
                for post in posts:
                    if len(images) >= count:
                        break
                    url = post.get("large_file_url") or post.get("file_url")
                    if self._valid_static_image_url(url):
                        data = await self._download_with_session(s, url)
                        if data:
                            images.append(data)
        except Exception as e:
            logger.error("[image-fetch] Danbooru failed: %s", e)
        return images

    @staticmethod
    def _valid_static_image_url(url: str | None) -> bool:
        if not url:
            return False
        return url.rsplit(".", 1)[-1].lower() in ("jpg", "jpeg", "png", "webp")

    @staticmethod
    async def _download_with_session(
        session: aiohttp.ClientSession, url: str | None, headers: dict | None = None
    ) -> bytes | None:
        if not url:
            return None
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status == 200:
                    return await r.read()
        except Exception:
            return None
        return None
