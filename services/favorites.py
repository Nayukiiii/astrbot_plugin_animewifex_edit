"""本命系统：用户挑 3 个本命，抽老婆时按 favorite_prob 概率优先出。

引导流程（作品 → 角色）：
  1) 输入「作品 关键词」搜本命的作品（按 list.txt 聚合）
  2) 选定作品后列出该作品的角色，回数字选；超过 9 个分页
  3) 选完一个回到「作品 关键词」搜下一个，直到 3 个或「跳过」
  4) 搜不到的作品提示去用「添老婆」申请

数据落在 records.json:
  favorites: {gid: {uid: {"chars": [...], "set_at": "YYYY-MM-DD", "intro_seen": bool, "tickets": int}}}

会话临时数据走内存：
  {gid: {uid: {"step": "picking", "mode": "work_search" | "char_search",
              "picked": [...], "current_work": "...", "candidates": [...],
              "char_pool": [...], "page": int, "kw": "", "expire_at": ts}}}
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime


SESSION_TTL = 600  # 10 分钟
PAGE_SIZE = 9


def _display(img: str) -> str:
    import os as _os
    name = _os.path.splitext(img)[0].split("/")[-1]
    if "!" in name:
        src, ch = name.split("!", 1)
        return f"《{src}》{ch}"
    return name


def _split(img: str) -> tuple[str, str]:
    import os as _os
    name = _os.path.splitext(img)[0].split("/")[-1]
    if "!" in name:
        s, c = name.split("!", 1)
        return s, c
    return "", name


class WorksIndex:
    """按作品聚合 list.txt 的索引：{source: [img, ...]}。

    提供作品搜索（关键词模糊匹配 source 名 + 翻译别名）和角色枚举。
    每次取 list_provider 重新构建，list.txt 缓存每小时刷新即可。
    """

    def __init__(self, list_provider, translation_get_fn=None):
        self.list_provider = list_provider
        self.translation_get_fn = translation_get_fn  # (char, source) -> profile dict or None

    def _build(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for line in (self.list_provider() or []):
            s, _ = _split(line)
            if not s:
                continue
            out.setdefault(s, []).append(line)
        return out

    def search_works(self, kw: str) -> list[str]:
        """匹配 source 名（原文 + 翻译别名）。返回 source 列表。"""
        kw = (kw or "").strip().lower()
        if not kw:
            return []
        works = self._build()
        out = []
        for src in works.keys():
            if kw in src.lower():
                out.append(src)
                continue
            # 用第一个角色的翻译档案的 source 别名匹配
            if self.translation_get_fn:
                chars = works[src]
                if chars:
                    _, first_char = _split(chars[0])
                    profile = None
                    try:
                        profile = self.translation_get_fn(first_char, src)
                    except Exception:
                        profile = None
                    if profile:
                        aliases = [
                            (profile.get("en_source") or ""),
                            (profile.get("ja_source") or ""),
                            (profile.get("short_source") or ""),
                        ]
                        if any(kw in a.lower() for a in aliases if a):
                            out.append(src)
        # 去重保序
        seen, dedup = set(), []
        for s in out:
            if s not in seen:
                seen.add(s)
                dedup.append(s)
        return dedup

    def chars_of(self, source: str) -> list[str]:
        """返回该作品下的所有 img 路径。"""
        return list(self._build().get(source, []))


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
        translation_get_fn=None,
    ):
        self.records = records
        self.list_provider = list_provider
        self.save_records_fn = save_records_fn
        self.get_today_fn = get_today_fn
        self.favorite_prob = float(favorite_prob or 0)
        self.change_cooldown_days = int(change_cooldown_days or 0)
        self.works = WorksIndex(list_provider, translation_get_fn=translation_get_fn)
        self._sessions: dict = {}  # {gid: {uid: session_dict}}

    # ----- 存储 -----
    def _bucket(self) -> dict:
        return self.records.setdefault("favorites", {})

    def get_record(self, gid: str, uid: str) -> dict:
        bucket = self._bucket()
        grp = bucket.setdefault(gid, {})
        rec = grp.setdefault(
            uid,
            {"chars": [], "set_at": "", "intro_seen": False, "tickets": 0},
        )
        # 跨群继承：本群没设过（chars=[] 且 set_at=""）→ 用最近设过的群作为本命
        # 让"本命"按账号生效，而不是按群。已设过/换过的群不再继承。
        if not rec.get("chars") and not rec.get("set_at"):
            best = None
            for other_gid, users in bucket.items():
                if other_gid == gid:
                    continue
                o = users.get(uid)
                if o and o.get("chars"):
                    if best is None or (o.get("set_at", "") or "") > (best.get("set_at", "") or ""):
                        best = o
            if best:
                rec["chars"] = list(best.get("chars") or [])
                rec["set_at"] = best.get("set_at", "")
                rec["intro_seen"] = True
                try:
                    self.save_records_fn()
                except Exception:
                    pass
        return rec

    def has_favorites(self, gid: str, uid: str) -> bool:
        # 走 get_record 以触发跨群继承
        return bool(self.get_record(gid, uid).get("chars"))

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
            "mode": "work_search",
            "picked": [],
            "current_work": "",
            "candidates": [],   # 当前作品/角色列表 (img path 或 source 名)
            "page": 0,
            "kw": "",
            "expire_at": time.time() + SESSION_TTL,
        }
        self._sessions.setdefault(gid, {})[uid] = s
        return s

    def clear_session(self, gid: str, uid: str) -> None:
        self._sessions.get(gid, {}).pop(uid, None)

    def touch_session(self, s: dict) -> None:
        s["expire_at"] = time.time() + SESSION_TTL

    # ----- 显示辅助 -----
    @staticmethod
    def _display(img: str) -> str:
        return _display(img)

    @staticmethod
    def _split(img: str):
        return _split(img)

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
        # 走 get_record 触发跨群继承
        rec = self.get_record(gid, uid)
        chars = [c for c in (rec.get("chars") or []) if c]
        if not chars or self.favorite_prob <= 0:
            return None
        all_lines = set(self.list_provider() or [])
        chars = [c for c in chars if c in all_lines]
        if not chars:
            return None
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
