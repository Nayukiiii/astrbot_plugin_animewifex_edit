from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.github_publisher import GitHubPublisher
from services.image_fetcher import ImageFetcher
from services.retention import RetentionService
from services.review import ReviewStatus
from services.translation import TranslationCache


class FakeImageFetcher(ImageFetcher):
    def __init__(self):
        super().__init__({}, translate_fn=self.translate)
        self.calls: list[tuple[str, str]] = []

    async def translate(self, char: str, source: str) -> dict:
        return {
            "en_char": "Shion",
            "ja_char": "紫苑",
            "kana_char": "しおん",
            "alt_char": ["Shi-on"],
            "en_source": "Eden",
            "ja_source": "eden*",
            "short_source": "eden",
        }

    async def gelbooru_fetch(self, query: str, count: int) -> list[bytes]:
        self.calls.append(("gel", query))
        return []

    async def yandere_fetch(self, query: str, count: int) -> list[bytes]:
        self.calls.append(("yan", query))
        return []

    async def danbooru_fetch(self, query: str, count: int) -> list[bytes]:
        self.calls.append(("dan", query))
        if query == "shion":
            return [b"\xff\xd8\xfffake-jpeg"]
        return []

    async def konachan_fetch(self, query: str, count: int) -> list[bytes]:
        self.calls.append(("kon", query))
        return []

    async def vndb_fetch(self, char_name: str, source: str, count: int) -> list[bytes]:
        self.calls.append(("vndb", char_name))
        return []

    async def getchu_fetch(self, source_query: str, count: int) -> list[bytes]:
        self.calls.append(("getchu", source_query))
        return []

    async def dlsite_cover_fetch(self, source_query: str, count: int) -> list[bytes]:
        self.calls.append(("dlsite", source_query))
        return []


def run_translation_cache(tmp: Path) -> None:
    cache = TranslationCache(str(tmp / "en_cache.json"))
    cache.write_profile(
        "紫苑",
        "eden*",
        {
            "zh_char": "紫苑",
            "en_char": "Shion",
            "ja_char": "紫苑",
            "alt_char": ["Shi-on"],
            "en_source": "Eden",
        },
    )
    profile = cache.get_profile("紫苑", "eden*")
    assert profile["en_char"] == "Shion"
    assert cache.get_en_name("紫苑", "eden*") == "Shion"
    assert cache.remove("紫苑", "eden*") == 1


def run_retention() -> None:
    records = {"change": {}, "ntr": {}, "swap": {}, "draw_stats": {}}
    drawn_pool = {"g1": {"u1": ["img2/Eden!Shion.jpg", "img2/Eden!Shion.jpg", "img2/Eden!Maya.jpg"]}}
    saves = {"count": 0}

    service = RetentionService(
        records,
        drawn_pool,
        list_cache_size_fn=lambda: 4,
        save_records_fn=lambda: saves.__setitem__("count", saves["count"] + 1),
        get_today_fn=lambda: "2026-05-06",
        change_limit=2,
        ntr_limit=1,
        swap_limit=3,
    )

    service.record_daily_draw("g1", "u1", "2026-05-05")
    service.record_daily_draw("g1", "u1", "2026-05-06")
    service.record_daily_draw("g1", "u1", "2026-05-06")
    stats = service.get_draw_stats("g1", "u1")
    assert stats["streak"] == 2
    assert stats["total_draws"] == 2
    assert saves["count"] == 2
    assert service.album_summary("g1", "u1") == (2, 4, 50)
    assert service.wife_display_name("img2/Eden!Shion.jpg") == "《Eden》Shion"

    group_config = {"u1": ["img2/Eden!Shion.jpg", "2026-05-06", "Nayuki"]}
    assert service.today_wife_rows(group_config, "2026-05-06") == [("Nayuki", "《Eden》Shion")]
    assert "连续 2 天" in service.retention_hint("g1", "u1")


def run_github_publisher(tmp: Path) -> None:
    list_cache = tmp / "list_cache.txt"
    list_cache.write_text("img2/Eden!Shion.jpg\nimg3/Other!Alice.png\n", encoding="utf-8")
    publisher = GitHubPublisher(
        {"github_branch": "main"},
        str(list_cache),
        translation_profile_fn=lambda char, source: {"en_char": "Shion"} if char == "紫苑" else {},
    )

    assert publisher.get_img_dir("Eden") == "img2"
    assert publisher.detect_img_ext(b"\x89PNG\r\n\x1a\nxxx") == ".png"
    assert publisher.detect_img_ext(b"\xff\xd8\xffxxx") == ".jpg"
    assert publisher.detect_img_ext(b"RIFFxxxxWEBPxxx") == ".webp"
    assert publisher._safe_branch_part(publisher._branch_char("eden*", "紫苑")) == "Shion"


def run_review_status() -> None:
    assert ReviewStatus.label(ReviewStatus.NEED_SOURCE) == "待补来源"
    assert ReviewStatus.PR_CREATED in ReviewStatus.LOCKED
    assert ReviewStatus.REJECTED in ReviewStatus.DONE


async def run_image_fetcher_order() -> None:
    fetcher = FakeImageFetcher()
    images = await fetcher.fetch_character_images("紫苑", "eden*", count=1)
    assert images == [b"\xff\xd8\xfffake-jpeg"]

    combo_index = next(i for i, call in enumerate(fetcher.calls) if call == ("gel", "shion eden"))
    vndb_index = next(i for i, call in enumerate(fetcher.calls) if call == ("vndb", "紫苑"))
    char_index = next(i for i, call in enumerate(fetcher.calls) if call == ("dan", "shion"))
    assert combo_index < vndb_index < char_index
    assert not any(source in {"getchu", "dlsite"} for source, _ in fetcher.calls)


async def main() -> None:
    tmp = ROOT / ".codex_dry_run_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        run_translation_cache(tmp)
        run_retention()
        run_github_publisher(tmp)
        run_review_status()
        await run_image_fetcher_order()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("dry-run ok: translation, retention, review, image source order, github publisher")


if __name__ == "__main__":
    asyncio.run(main())
