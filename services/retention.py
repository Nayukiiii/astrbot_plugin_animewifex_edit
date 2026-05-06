from __future__ import annotations

from datetime import datetime, timedelta
import os


class RetentionService:
    """Small, testable helpers for daily draw retention and album progress."""

    def __init__(
        self,
        records: dict,
        drawn_pool: dict,
        list_cache_size_fn,
        save_records_fn,
        get_today_fn,
        *,
        change_limit: int,
        ntr_limit: int,
        swap_limit: int,
    ):
        self.records = records
        self.drawn_pool = drawn_pool
        self.list_cache_size_fn = list_cache_size_fn
        self.save_records_fn = save_records_fn
        self.get_today_fn = get_today_fn
        self.change_limit = change_limit or 0
        self.ntr_limit = ntr_limit or 0
        self.swap_limit = swap_limit or 0

    def record_daily_draw(self, gid: str, uid: str, today: str) -> dict:
        grp = self.records.setdefault("draw_stats", {}).setdefault(gid, {})
        rec = grp.get(uid, {"last_date": "", "streak": 0, "total_draws": 0})
        if rec.get("last_date") == today:
            return rec

        yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
        streak = int(rec.get("streak", 0) or 0)
        if rec.get("last_date") == yesterday:
            streak += 1
        else:
            streak = 1

        rec = {
            "last_date": today,
            "streak": streak,
            "total_draws": int(rec.get("total_draws", 0) or 0) + 1,
        }
        grp[uid] = rec
        self.save_records_fn()
        return rec

    def get_draw_stats(self, gid: str, uid: str) -> dict:
        return self.records.setdefault("draw_stats", {}).setdefault(gid, {}).get(
            uid, {"last_date": "", "streak": 0, "total_draws": 0}
        )

    def album_summary(self, gid: str, uid: str) -> tuple[int, int, int]:
        seen = len(set(self.drawn_pool.get(gid, {}).get(uid, [])))
        total = int(self.list_cache_size_fn() or 0)
        pct = int(seen * 100 / total) if total else 0
        return seen, total, pct

    def remaining_daily_count(self, bucket: str, gid: str, uid: str, limit: int, today: str) -> int:
        rec = self.records.get(bucket, {}).get(gid, {}).get(uid, {})
        used = int(rec.get("count", 0) or 0) if rec.get("date") == today else 0
        return max(0, int(limit or 0) - used)

    def retention_hint(self, gid: str, uid: str) -> str:
        today = self.get_today_fn()
        stats = self.get_draw_stats(gid, uid)
        seen, total, pct = self.album_summary(gid, uid)
        change_left = self.remaining_daily_count("change", gid, uid, self.change_limit, today)
        ntr_left = self.remaining_daily_count("ntr", gid, uid, self.ntr_limit, today)
        swap_left = self.remaining_daily_count("swap", gid, uid, self.swap_limit, today)

        progress = f"图鉴：已见 {seen}/{total}（{pct}%）" if total else f"图鉴：已见 {seen} 位"
        streak = int(stats.get("streak", 0) or 0)
        tips = [
            f"{progress}，连续 {streak} 天。",
            f"今日可玩：要本子 / 换老婆 {change_left} 次 / 牛老婆 {ntr_left} 次 / 交换 {swap_left} 次。",
            "想把本命塞进池子：添老婆 角色名/作品名",
        ]
        return "\n" + "\n".join(tips)

    @staticmethod
    def wife_display_name(img: str) -> str:
        name = os.path.splitext(img)[0].split("/")[-1]
        if "!" in name:
            source, chara = name.split("!", 1)
            return f"《{source}》{chara}"
        return name

    def today_wife_rows(self, group_config: dict, today: str) -> list[tuple[str, str]]:
        rows = []
        for data in group_config.values():
            if not isinstance(data, list) or len(data) < 3 or data[1] != today:
                continue
            rows.append((data[2], self.wife_display_name(data[0])))
        return rows

    def draw_streak_rank_rows(self, gid: str, group_config: dict, limit: int = 10) -> list[tuple[int, int, str]]:
        rows = []
        for uid, rec in self.records.get("draw_stats", {}).get(gid, {}).items():
            streak = int(rec.get("streak", 0) or 0)
            total = int(rec.get("total_draws", 0) or 0)
            if streak <= 0 and total <= 0:
                continue
            nick = (group_config.get(uid) or [None, None, uid])[2]
            rows.append((streak, total, nick))
        rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return rows[:limit]

    def album_rank_rows(self, gid: str, group_config: dict, limit: int = 10) -> tuple[list[tuple[int, int, str]], int]:
        total = int(self.list_cache_size_fn() or 0)
        rows = []
        for uid, pool in self.drawn_pool.get(gid, {}).items():
            seen = len(set(pool))
            if seen <= 0:
                continue
            nick = (group_config.get(uid) or [None, None, uid])[2]
            pct = int(seen * 100 / total) if total else 0
            rows.append((seen, pct, nick))
        rows.sort(key=lambda x: x[0], reverse=True)
        return rows[:limit], total
