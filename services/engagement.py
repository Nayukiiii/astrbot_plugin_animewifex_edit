"""留存增强：里程碑奖励、补签券、每日任务、作品图鉴、周榜结算。

所有数据共用 records.json：
  milestones: {gid: {uid: [milestone_id, ...]}}
  streak_freeze: {gid: {uid: {"tokens": int, "last_grant_week": "YYYY-Www"}}}
  daily_quests: {gid: {uid: {"date": "YYYY-MM-DD", "quests": [...], "claimed": bool}}}
  weekly_settle: {gid: {"last_week": "YYYY-Www", "disabled": bool}}
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta


def _today_dt(today: str) -> datetime:
    return datetime.fromisoformat(today)


def _iso_week(today: str) -> str:
    y, w, _ = _today_dt(today).isocalendar()
    return f"{y}-W{w:02d}"


def _last_iso_week(today: str) -> str:
    last = _today_dt(today) - timedelta(days=7)
    y, w, _ = last.isocalendar()
    return f"{y}-W{w:02d}"


# ---------- 里程碑 ----------

MILESTONE_DEFS = [
    # (id, type, threshold, reward_text, rewards)
    ("streak_7",   "streak", 7,   "连签 7 天",   {"freeze_ticket": 1}),
    ("streak_30",  "streak", 30,  "连签 30 天",  {"freeze_ticket": 1, "favorite_ticket": 1}),
    ("streak_100", "streak", 100, "连签 100 天", {"freeze_ticket": 3, "favorite_ticket": 1, "title": "签到大师"}),
    ("album_10",   "album_pct", 10,  "图鉴进度 10%",  {"freeze_ticket": 1}),
    ("album_50",   "album_pct", 50,  "图鉴进度 50%",  {"favorite_ticket": 1}),
    ("album_100",  "album_pct", 100, "图鉴大满贯",    {"favorite_ticket": 2, "title": "图鉴大师"}),
]


class MilestoneService:
    def __init__(self, records: dict, save_records_fn, favorites_service):
        self.records = records
        self.save_records_fn = save_records_fn
        self.favorites = favorites_service

    def _bucket(self) -> dict:
        return self.records.setdefault("milestones", {})

    def _seen(self, gid: str, uid: str) -> list:
        return self._bucket().setdefault(gid, {}).setdefault(uid, [])

    def check(self, gid: str, uid: str, streak: int, album_pct: int) -> list[dict]:
        """返回本次新触发的里程碑列表，调用方负责发提示和发奖励。"""
        seen = self._seen(gid, uid)
        newly = []
        for mid, mtype, thr, desc, rewards in MILESTONE_DEFS:
            if mid in seen:
                continue
            if mtype == "streak" and streak >= thr:
                pass
            elif mtype == "album_pct" and album_pct >= thr:
                pass
            else:
                continue
            seen.append(mid)
            self._apply_rewards(gid, uid, rewards)
            newly.append({"id": mid, "desc": desc, "rewards": rewards})
        if newly:
            self.save_records_fn()
        return newly

    def _apply_rewards(self, gid: str, uid: str, rewards: dict) -> None:
        if rewards.get("freeze_ticket"):
            sf = self.records.setdefault("streak_freeze", {}).setdefault(gid, {})
            cur = sf.setdefault(uid, {"tokens": 0, "last_grant_week": ""})
            cur["tokens"] = int(cur.get("tokens", 0) or 0) + int(rewards["freeze_ticket"])
        if rewards.get("favorite_ticket"):
            self.favorites.add_ticket(gid, uid, int(rewards["favorite_ticket"]))
        if rewards.get("title"):
            titles = self.records.setdefault("titles", {}).setdefault(gid, {}).setdefault(uid, [])
            if rewards["title"] not in titles:
                titles.append(rewards["title"])


# ---------- 补签券 ----------

class StreakFreezeService:
    def __init__(self, records: dict, save_records_fn, get_today_fn, weekly_grant: int = 1):
        self.records = records
        self.save_records_fn = save_records_fn
        self.get_today_fn = get_today_fn
        self.weekly_grant = int(weekly_grant or 0)

    def _bucket(self) -> dict:
        return self.records.setdefault("streak_freeze", {})

    def get(self, gid: str, uid: str) -> dict:
        return self._bucket().setdefault(gid, {}).setdefault(
            uid, {"tokens": 0, "last_grant_week": ""}
        )

    def tokens(self, gid: str, uid: str) -> int:
        return int(self.get(gid, uid).get("tokens", 0) or 0)

    def grant_weekly_if_due(self, gid: str, uid: str) -> int:
        """每周首抽时调用，按周号去重。返回本次新发数量。"""
        if self.weekly_grant <= 0:
            return 0
        cur = self.get(gid, uid)
        wk = _iso_week(self.get_today_fn())
        if cur.get("last_grant_week") == wk:
            return 0
        cur["tokens"] = int(cur.get("tokens", 0) or 0) + self.weekly_grant
        cur["last_grant_week"] = wk
        self.save_records_fn()
        return self.weekly_grant

    def consume(self, gid: str, uid: str) -> bool:
        cur = self.get(gid, uid)
        if int(cur.get("tokens", 0) or 0) <= 0:
            return False
        cur["tokens"] -= 1
        self.save_records_fn()
        return True

    def streak_at_risk(self, gid: str, uid: str) -> bool:
        """昨天签过但今天还没签 → 风险。需要 draw_stats 数据。"""
        today = self.get_today_fn()
        ds = self.records.get("draw_stats", {}).get(gid, {}).get(uid)
        if not ds:
            return False
        if ds.get("last_date") == today:
            return False
        yest = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
        return ds.get("last_date") == yest and int(ds.get("streak", 0) or 0) >= 1

    def apply_freeze(self, gid: str, uid: str) -> tuple[bool, int]:
        """消耗一张券，把 last_date 推到昨天，让今天的抽老婆保住连签。"""
        if not self.consume(gid, uid):
            return False, 0
        today = self.get_today_fn()
        yest = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
        ds = self.records.setdefault("draw_stats", {}).setdefault(gid, {}).setdefault(
            uid, {"last_date": "", "streak": 0, "total_draws": 0}
        )
        ds["last_date"] = yest
        # streak 保持不变，下次抽老婆 record_daily_draw 会接上
        self.save_records_fn()
        return True, int(ds.get("streak", 0) or 0)


# ---------- 每日任务 ----------

QUEST_POOL = [
    {"id": "draw_once",   "desc": "抽老婆 1 次",   "target": 1, "trigger": "draw"},
    {"id": "change",      "desc": "用一次「换老婆」", "target": 1, "trigger": "change"},
    {"id": "ntr_try",     "desc": "尝试一次「牛老婆」", "target": 1, "trigger": "ntr"},
    {"id": "swap_done",   "desc": "完成一次「交换老婆」", "target": 1, "trigger": "swap_done"},
    {"id": "hentai",      "desc": "用一次「要本子」", "target": 1, "trigger": "hentai"},
    {"id": "inspect",     "desc": "解析角色 1 次", "target": 1, "trigger": "inspect"},
    {"id": "today_board", "desc": "查看「今日老婆榜」",  "target": 1, "trigger": "board"},
]
DAILY_QUEST_COUNT = 3
COMPLETE_REQUIRED = 2
REWARDS_ON_CLAIM = {"freeze_ticket": 1}


class DailyQuestService:
    def __init__(self, records: dict, save_records_fn, get_today_fn, freeze_service):
        self.records = records
        self.save_records_fn = save_records_fn
        self.get_today_fn = get_today_fn
        self.freeze = freeze_service

    def _bucket(self) -> dict:
        return self.records.setdefault("daily_quests", {})

    def _ensure_today(self, gid: str, uid: str) -> dict:
        today = self.get_today_fn()
        rec = self._bucket().setdefault(gid, {}).get(uid)
        if not rec or rec.get("date") != today:
            pool = list(QUEST_POOL)
            secrets.SystemRandom().shuffle(pool)
            chosen = []
            seen_triggers = set()
            for q in pool:
                if q["trigger"] in seen_triggers:
                    continue
                chosen.append({**q, "progress": 0, "done": False})
                seen_triggers.add(q["trigger"])
                if len(chosen) >= DAILY_QUEST_COUNT:
                    break
            rec = {"date": today, "quests": chosen, "claimed": False}
            self._bucket()[gid][uid] = rec
            self.save_records_fn()
        return rec

    def get(self, gid: str, uid: str) -> dict:
        return self._ensure_today(gid, uid)

    def mark(self, gid: str, uid: str, trigger: str) -> list[str]:
        """触发某行为，回报本次因此完成的任务 desc 列表。"""
        rec = self._ensure_today(gid, uid)
        newly_done = []
        for q in rec["quests"]:
            if q.get("done") or q.get("trigger") != trigger:
                continue
            q["progress"] = int(q.get("progress", 0) or 0) + 1
            if q["progress"] >= q["target"]:
                q["done"] = True
                newly_done.append(q["desc"])
        if newly_done:
            self.save_records_fn()
        return newly_done

    def can_claim(self, gid: str, uid: str) -> tuple[bool, int, int]:
        rec = self._ensure_today(gid, uid)
        if rec.get("claimed"):
            return False, sum(1 for q in rec["quests"] if q.get("done")), len(rec["quests"])
        done = sum(1 for q in rec["quests"] if q.get("done"))
        return done >= COMPLETE_REQUIRED, done, len(rec["quests"])

    def claim(self, gid: str, uid: str) -> tuple[bool, str]:
        ok, done, _ = self.can_claim(gid, uid)
        if not ok:
            return False, f"完成 {done}/{COMPLETE_REQUIRED} 个任务后再来领~"
        rec = self._bucket()[gid][uid]
        rec["claimed"] = True
        # 发奖励
        if REWARDS_ON_CLAIM.get("freeze_ticket"):
            cur = self.records.setdefault("streak_freeze", {}).setdefault(gid, {}).setdefault(
                uid, {"tokens": 0, "last_grant_week": ""}
            )
            cur["tokens"] = int(cur.get("tokens", 0) or 0) + REWARDS_ON_CLAIM["freeze_ticket"]
        self.save_records_fn()
        return True, "已领取：补签券 ×1"

    def render(self, gid: str, uid: str) -> str:
        rec = self._ensure_today(gid, uid)
        lines = ["今日任务（完成任意 2 个可领奖）："]
        for i, q in enumerate(rec["quests"], 1):
            tick = "✅" if q.get("done") else "⬜"
            lines.append(f"{i}. {tick} {q['desc']}")
        done = sum(1 for q in rec["quests"] if q.get("done"))
        if rec.get("claimed"):
            lines.append("奖励已领取 ✅")
        elif done >= COMPLETE_REQUIRED:
            lines.append("可领取！发送「领取任务奖励」拿走~")
        else:
            lines.append(f"已完成 {done}/{COMPLETE_REQUIRED}")
        return "\n".join(lines)


# ---------- 作品图鉴 ----------

def parse_work(img: str) -> tuple[str, str]:
    """img -> (source, char_name)"""
    name = os.path.splitext(img)[0].split("/")[-1]
    if "!" in name:
        s, c = name.split("!", 1)
        return s, c
    return "", name


class WorksAlbumService:
    def __init__(self, drawn_pool: dict, list_provider, records: dict, save_records_fn, favorites_service):
        self.drawn_pool = drawn_pool
        self.list_provider = list_provider
        self.records = records
        self.save_records_fn = save_records_fn
        self.favorites = favorites_service

    def _index(self) -> dict[str, set[str]]:
        works: dict[str, set[str]] = {}
        for line in (self.list_provider() or []):
            s, c = parse_work(line)
            if not s:
                continue
            works.setdefault(s, set()).add(c)
        return works

    def user_progress(self, gid: str, uid: str) -> list[tuple[str, int, int, bool]]:
        """返回 [(source, owned, total, done)]，按 owned 倒序。"""
        works = self._index()
        owned: dict[str, set[str]] = {}
        for img in self.drawn_pool.get(gid, {}).get(uid, []):
            s, c = parse_work(img)
            if s:
                owned.setdefault(s, set()).add(c)
        out = []
        for s, chars in works.items():
            o = len(owned.get(s, set()) & chars)
            if o <= 0:
                continue
            out.append((s, o, len(chars), o == len(chars)))
        out.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return out

    def work_detail(self, gid: str, uid: str, source: str) -> tuple[set[str], set[str]]:
        """返回 (已收集集合, 全集合)，找不到作品返回 (空, 空)"""
        works = self._index()
        full = works.get(source)
        if not full:
            return set(), set()
        owned = {parse_work(img)[1] for img in self.drawn_pool.get(gid, {}).get(uid, [])
                 if parse_work(img)[0] == source}
        return owned & full, full

    def check_completion(self, gid: str, uid: str) -> list[str]:
        """检测新完成的作品（用 records.works_done 去重），返回新作品名列表。"""
        done_bucket = self.records.setdefault("works_done", {}).setdefault(gid, {}).setdefault(uid, [])
        newly = []
        for s, o, t, done in self.user_progress(gid, uid):
            if done and s not in done_bucket:
                done_bucket.append(s)
                newly.append(s)
                # 奖励：每集齐一部作品给 1 张换本命券
                self.favorites.add_ticket(gid, uid, 1)
        if newly:
            self.save_records_fn()
        return newly


# ---------- 周榜结算 ----------

class WeeklySettleService:
    def __init__(self, records: dict, drawn_pool: dict, save_records_fn, get_today_fn, default_enabled: bool = True):
        self.records = records
        self.drawn_pool = drawn_pool
        self.save_records_fn = save_records_fn
        self.get_today_fn = get_today_fn
        self.default_enabled = bool(default_enabled)

    def _bucket(self) -> dict:
        return self.records.setdefault("weekly_settle", {})

    def is_enabled(self, gid: str) -> bool:
        rec = self._bucket().get(gid, {})
        if "disabled" in rec:
            return not rec["disabled"]
        return self.default_enabled

    def set_enabled(self, gid: str, enabled: bool) -> None:
        rec = self._bucket().setdefault(gid, {})
        rec["disabled"] = not enabled
        self.save_records_fn()

    def needs_settle(self, gid: str) -> bool:
        if not self.is_enabled(gid):
            return False
        today = self.get_today_fn()
        last_week = _last_iso_week(today)
        rec = self._bucket().get(gid, {})
        return rec.get("last_week") != last_week

    def mark_settled(self, gid: str) -> None:
        today = self.get_today_fn()
        last_week = _last_iso_week(today)
        rec = self._bucket().setdefault(gid, {})
        rec["last_week"] = last_week
        self.save_records_fn()

    def build_report(self, gid: str, group_config: dict) -> str | None:
        """汇总上周战报：连签、图鉴、牛榜 Top3。"""
        rows_streak = []
        for uid, ds in self.records.get("draw_stats", {}).get(gid, {}).items():
            streak = int(ds.get("streak", 0) or 0)
            if streak <= 0:
                continue
            nick = (group_config.get(uid) or [None, None, uid])[2]
            rows_streak.append((streak, nick))
        rows_streak.sort(reverse=True)

        rows_album = []
        for uid, pool in self.drawn_pool.get(gid, {}).items():
            seen = len(set(pool))
            if seen <= 0:
                continue
            nick = (group_config.get(uid) or [None, None, uid])[2]
            rows_album.append((seen, nick))
        rows_album.sort(reverse=True)

        if not rows_streak and not rows_album:
            return None

        lines = ["📅 上周战报"]
        if rows_streak:
            lines.append("\n🔥 连签榜")
            for i, (s, n) in enumerate(rows_streak[:3], 1):
                lines.append(f"  {i}. {n} —— 连签 {s} 天")
        if rows_album:
            lines.append("\n📚 图鉴榜")
            for i, (a, n) in enumerate(rows_album[:3], 1):
                lines.append(f"  {i}. {n} —— 已见 {a} 位")
        lines.append("\n（每周一播报，发「关闭周榜」可关闭）")
        return "\n".join(lines)
