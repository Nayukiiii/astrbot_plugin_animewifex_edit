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
from services.favorites import FavoritesService
from services.engagement import (
    MilestoneService,
    StreakFreezeService,
    DailyQuestService,
    WorksAlbumService,
    WeeklySettleService,
)
from services.bonds import BondsService
from services.season import SeasonService


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


def run_engagement_suite() -> None:
    records = {"draw_stats": {"g1": {"u1": {"last_date": "2026-05-05", "streak": 6, "total_draws": 6}}}}
    drawn_pool = {"g1": {"u1": ["A!a.jpg", "A!b.jpg", "B!x.jpg"]}}
    saves = {"n": 0}
    save = lambda: saves.__setitem__("n", saves["n"] + 1)
    today_fn = lambda: "2026-05-06"
    list_provider = lambda: ["A!a.jpg", "A!b.jpg", "A!c.jpg", "B!x.jpg", "B!y.jpg"]

    fav = FavoritesService(records, list_provider, save, today_fn, favorite_prob=0, change_cooldown_days=30)
    assert not fav.has_favorites("g1", "u1")
    fav.start_session("g1", "u1")
    assert fav.session("g1", "u1") is not None
    fav.commit_picks("g1", "u1", ["A!a.jpg", "A!b.jpg"])
    assert fav.has_favorites("g1", "u1")
    # 作品先行：通过 works 索引搜
    assert "A" in fav.works.search_works("a")
    assert fav.works.chars_of("A") == ["A!a.jpg", "A!b.jpg", "A!c.jpg"]
    # roll_favorite with prob=0 should return None
    assert fav.roll_favorite("g1", "u1", drawn_pool) is None

    freeze = StreakFreezeService(records, save, today_fn, weekly_grant=1)
    granted = freeze.grant_weekly_if_due("g1", "u1")
    assert granted == 1
    assert freeze.grant_weekly_if_due("g1", "u1") == 0  # 同周不重发
    assert freeze.tokens("g1", "u1") == 1
    assert freeze.streak_at_risk("g1", "u1") is True  # 昨天签今天没签
    assert freeze.apply_freeze("g1", "u1") == (True, 6)
    assert freeze.tokens("g1", "u1") == 0

    ms = MilestoneService(records, save, fav)
    newly = ms.check("g1", "u1", streak=7, album_pct=10)
    assert any(m["id"] == "streak_7" for m in newly)
    assert any(m["id"] == "album_10" for m in newly)
    # 重复触发不再发
    assert ms.check("g1", "u1", streak=7, album_pct=10) == []

    quests = DailyQuestService(records, save, today_fn, freeze)
    q = quests.get("g1", "u2")
    assert len(q["quests"]) == 3
    # 标记 draw 触发器
    triggers = [qq["trigger"] for qq in q["quests"]]
    if "draw" in triggers:
        done = quests.mark("g1", "u2", "draw")
        assert done  # 至少完成 draw 1 个

    works = WorksAlbumService(drawn_pool, list_provider, records, save, fav)
    progress = works.user_progress("g1", "u1")
    sources = {row[0] for row in progress}
    assert "A" in sources
    new_done = works.check_completion("g1", "u1")
    # A 还差 c，B 还差 y，因此没新通关
    assert new_done == []

    weekly = WeeklySettleService(records, drawn_pool, save, today_fn, default_enabled=True)
    assert weekly.is_enabled("g1") is True
    assert weekly.needs_settle("g1") is True
    report = weekly.build_report("g1", {"u1": ["A!a.jpg", "2026-05-06", "Alice"]})
    assert report and "连签榜" in report
    weekly.mark_settled("g1")
    assert weekly.needs_settle("g1") is False
    weekly.set_enabled("g1", False)
    assert weekly.is_enabled("g1") is False


def run_bonds_suite() -> None:
    saves = {"n": 0}
    save = lambda: saves.__setitem__("n", saves["n"] + 1)
    store: dict = {}
    bonds = BondsService(store, save, lambda: "2026-05-06")
    # 5 次交换 → "互通有无"
    for _ in range(5):
        bonds.record("g1", "u1", "u2", "swap")
    rows = bonds.list_for_user("g1", "u1")
    assert rows and rows[0]["swap"] == 5
    assert "互通有无" in rows[0]["titles"]
    # 3 次单向 NTR → 主动方 "NTR大魔王"
    for _ in range(3):
        bonds.record("g1", "u1", "u2", "ntr")
    rows = bonds.list_for_user("g1", "u1")
    assert "NTR大魔王" in rows[0]["titles"]
    rows_b = bonds.list_for_user("g1", "u2")
    assert "NTR大魔王" not in rows_b[0]["titles"]


def run_season_suite() -> None:
    import json as _json
    raw = _json.dumps({
        "name": "测试季",
        "start": "2026-01-01",
        "end": "2026-12-31",
        "chars": ["S!x.jpg"],
        "rate_up": 1.0,
    })
    season = SeasonService(raw, lambda: "2026-05-06")
    assert season.is_active() is True
    assert season.filter_pool(["A!a.jpg", "S!x.jpg"]) == ["A!a.jpg", "S!x.jpg"]  # 期间内不剔除
    # rate=1 必定触发
    assert season.roll({}, "g1", "u1") == "S!x.jpg"

    raw2 = _json.dumps({
        "name": "过期季",
        "start": "2020-01-01",
        "end": "2020-12-31",
        "chars": ["S!x.jpg"],
        "rate_up": 1.0,
    })
    season2 = SeasonService(raw2, lambda: "2026-05-06")
    assert season2.is_active() is False
    assert "S!x.jpg" not in season2.filter_pool(["A!a.jpg", "S!x.jpg"])  # 期间外剔除
    assert season2.roll({}, "g1", "u1") is None


async def main() -> None:
    tmp = ROOT / ".codex_dry_run_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        run_translation_cache(tmp)
        run_retention()
        run_github_publisher(tmp)
        run_review_status()
        run_engagement_suite()
        run_bonds_suite()
        run_season_suite()
        await run_image_fetcher_order()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("dry-run ok: translation, retention, review, image source order, github publisher, engagement, bonds, season")


if __name__ == "__main__":
    asyncio.run(main())
