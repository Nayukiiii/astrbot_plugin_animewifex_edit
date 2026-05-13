"""本命系统：用户挑 3 个本命，抽老婆时按 favorite_prob 概率优先出。

数据落在 records.json:
  favorites: {gid: {uid: {"chars": [...], "set_at": "YYYY-MM-DD", "intro_seen": bool, "tickets": int}}}

会话临时数据走内存（不持久化即可）：
  {gid: {uid: {"step": "...", "picked": [...], "candidates": [...], "expire_at": ts}}}
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime


SESSION_TTL = 600  # 10 分钟


class FavoritesService:
    MAX_PICKS = 3

    def __init__(
        self,
        records: dict,
        list_provider,
        save_records_fn,
        get_today_fn,
        *,
        favorite_prob: float = 0.25,
        change_cooldown_days: int = 30,
    ):
        self.records = records
        self.list_provider = list_provider  # callable -> list[str] of valid img paths
        self.save_records_fn = save_records_fn
        self.get_today_fn = get_today_fn
        self.favorite_prob = float(favorite_prob or 0)
        self.change_cooldown_days = int(change_cooldown_days or 0)
        self._sessions: dict = {}  # {gid: {uid: session_dict}}

    # ----- 存储 -----
    def _bucket(self) -> dict:
        return self.records.setdefault("favorites", {})

    def get_record(self, gid: str, uid: str) -> dict:
        return self._bucket().setdefault(gid, {}).setdefault(
            uid,
            {"chars": [], "set_at": "", "intro_seen": False, "tickets": 0},
        )

    def has_favorites(self, gid: str, uid: str) -> bool:
        rec = self._bucket().get(gid, {}).get(uid, {})
        return bool(rec.get("chars"))

    def intro_seen(self, gid: str, uid: str) -> bool:
        rec = self._bucket().get(gid, {}).get(uid, {})
        return bool(rec.get("intro_seen"))

    def mark_intro_seen(self, gid: str, uid: str) -> None:
        rec = self.get_record(gid, uid)
        if not rec.get("intro_seen"):
            rec["intro_seen"] = True
            self.save_records_fn()

    def add_ticket(self, gid: str, uid: str, n: int = 1) -> int:
        rec = self.get_record(gid, uid)
        rec["tickets"] = int(rec.get("tickets", 0) or 0) + n
        self.save_records_fn()
        return rec["tickets"]

    def use_ticket(self, gid: str, uid: str) -> bool:
        rec = self.get_record(gid, uid)
        if int(rec.get("tickets", 0) or 0) <= 0:
            return False
        rec["tickets"] -= 1
        self.save_records_fn()
        return True

    # ----- 引导会话 -----
    def session(self, gid: str, uid: str) -> dict | None:
        s = self._sessions.get(gid, {}).get(uid)
        if not s:
            return None
        if s.get("expire_at", 0) < time.time():
            self._sessions.get(gid, {}).pop(uid, None)
            return None
        return s

    def start_session(self, gid: str, uid: str) -> dict:
        s = {
            "step": "picking",
            "picked": [],
            "candidates": [],
            "expire_at": time.time() + SESSION_TTL,
        }
        self._sessions.setdefault(gid, {})[uid] = s
        return s

    def clear_session(self, gid: str, uid: str) -> None:
        self._sessions.get(gid, {}).pop(uid, None)

    def touch_session(self, s: dict) -> None:
        s["expire_at"] = time.time() + SESSION_TTL

    # ----- 搜索（仅在 list.txt 现有角色中搜） -----
    @staticmethod
    def _display(img: str) -> str:
        import os as _os
        name = _os.path.splitext(img)[0].split("/")[-1]
        if "!" in name:
            src, ch = name.split("!", 1)
            return f"《{src}》{ch}"
        return name

    def search(self, keyword: str, limit: int = 10) -> list[str]:
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        lines = self.list_provider() or []
        out = []
        for line in lines:
            disp = self._display(line).lower()
            if kw in disp or kw in line.lower():
                out.append(line)
                if len(out) >= limit:
                    break
        return out

    # ----- 保存本命 -----
    def commit_picks(self, gid: str, uid: str, picks: list[str]) -> dict:
        valid = [p for p in picks if p][: self.MAX_PICKS]
        rec = self.get_record(gid, uid)
        rec["chars"] = valid
        rec["set_at"] = self.get_today_fn()
        rec["intro_seen"] = True
        self.save_records_fn()
        return rec

    def can_change_for_free(self, gid: str, uid: str) -> tuple[bool, int]:
        rec = self.get_record(gid, uid)
        set_at = rec.get("set_at") or ""
        if not set_at:
            return True, 0
        try:
            d0 = datetime.fromisoformat(set_at).date()
            d1 = datetime.fromisoformat(self.get_today_fn()).date()
            days = (d1 - d0).days
        except Exception:
            return True, 0
        if days >= self.change_cooldown_days:
            return True, 0
        return False, self.change_cooldown_days - days

    # ----- 抽老婆时按概率出本命 -----
    def roll_favorite(self, gid: str, uid: str, drawn_pool: dict) -> str | None:
        rec = self._bucket().get(gid, {}).get(uid, {})
        chars = [c for c in (rec.get("chars") or []) if c]
        if not chars or self.favorite_prob <= 0:
            return None
        # 过滤 list.txt 里已不存在的
        all_lines = set(self.list_provider() or [])
        chars = [c for c in chars if c in all_lines]
        if not chars:
            return None
        # 过滤去重池里已抽过的
        drawn = set(drawn_pool.get(gid, {}).get(uid, []))
        avail = [c for c in chars if c not in drawn]
        if not avail:
            return None
        if not _secrets_roll(self.favorite_prob):
            return None
        return secrets.choice(avail)


def _secrets_roll(prob: float) -> bool:
    if prob <= 0:
        return False
    if prob >= 1:
        return True
    return secrets.randbelow(1_000_000) < int(prob * 1_000_000)
