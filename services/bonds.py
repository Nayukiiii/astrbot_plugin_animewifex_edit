"""CP / 羁绊系统。

存储独立于 records.json，落 bonds.json：
  {gid: {"uid_a|uid_b": {"swap": int, "ntr_a_to_b": int, "ntr_b_to_a": int, "last_at": "iso"}}}
key 用 sorted(uid_a, uid_b) 的 "min|max" 表示，是无向 pair。
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _pair_key(uid_a: str, uid_b: str) -> tuple[str, bool]:
    """返回 (key, a_is_min)。a_is_min=True 表示 uid_a 是 key 左边那个。"""
    if uid_a < uid_b:
        return f"{uid_a}|{uid_b}", True
    return f"{uid_b}|{uid_a}", False


class BondsService:
    ACTIVE_DAYS = 7

    def __init__(self, store: dict, save_fn, get_today_fn):
        self.store = store  # {gid: {key: rec}}
        self.save_fn = save_fn
        self.get_today_fn = get_today_fn

    def _group(self, gid: str) -> dict:
        return self.store.setdefault(gid, {})

    def _now_iso(self) -> str:
        return datetime.utcnow().isoformat()

    def record(self, gid: str, uid_a: str, uid_b: str, kind: str) -> None:
        """kind: 'swap' | 'ntr'  (a 主动作用于 b)"""
        if not uid_a or not uid_b or uid_a == uid_b:
            return
        key, a_is_min = _pair_key(uid_a, uid_b)
        grp = self._group(gid)
        rec = grp.setdefault(key, {"swap": 0, "ntr_a_to_b": 0, "ntr_b_to_a": 0, "last_at": ""})
        if kind == "swap":
            rec["swap"] = int(rec.get("swap", 0) or 0) + 1
        elif kind == "ntr":
            field = "ntr_a_to_b" if a_is_min else "ntr_b_to_a"
            rec[field] = int(rec.get(field, 0) or 0) + 1
        rec["last_at"] = self._now_iso()
        self.save_fn()

    def list_for_user(self, gid: str, uid: str) -> list[dict]:
        """返回该用户所有羁绊，附带对方 uid 和 title 列表。"""
        out = []
        for key, rec in self._group(gid).items():
            a, b = key.split("|", 1)
            if uid not in (a, b):
                continue
            other = b if uid == a else a
            titles = self._titles(rec, uid_is_min=(uid == a))
            out.append({
                "other": other,
                "swap": int(rec.get("swap", 0) or 0),
                "ntr_to_other": int(rec.get("ntr_a_to_b", 0) or 0) if uid == a else int(rec.get("ntr_b_to_a", 0) or 0),
                "ntr_from_other": int(rec.get("ntr_b_to_a", 0) or 0) if uid == a else int(rec.get("ntr_a_to_b", 0) or 0),
                "titles": titles,
                "last_at": rec.get("last_at", ""),
            })
        out.sort(key=lambda x: (x["swap"] + x["ntr_to_other"] + x["ntr_from_other"]), reverse=True)
        return out

    def active_titles(self, gid: str, uid: str) -> list[tuple[str, str]]:
        """活跃（last_at 在 ACTIVE_DAYS 内）的羁绊，返回 [(title, other_uid)]"""
        cutoff = datetime.utcnow() - timedelta(days=self.ACTIVE_DAYS)
        out = []
        for r in self.list_for_user(gid, uid):
            try:
                last = datetime.fromisoformat(r["last_at"]) if r["last_at"] else None
            except Exception:
                last = None
            if not last or last < cutoff:
                continue
            for t in r["titles"]:
                out.append((t, r["other"]))
        return out

    def _titles(self, rec: dict, uid_is_min: bool) -> list[str]:
        swap = int(rec.get("swap", 0) or 0)
        ntr_a = int(rec.get("ntr_a_to_b", 0) or 0)
        ntr_b = int(rec.get("ntr_b_to_a", 0) or 0)
        titles = []
        if swap >= 5:
            titles.append("互通有无")
        my_ntr = ntr_a if uid_is_min else ntr_b
        if my_ntr >= 3:
            titles.append("NTR大魔王")
        if swap + ntr_a + ntr_b >= 10 and ntr_a >= 1 and ntr_b >= 1:
            titles.append("相爱相杀")
        return titles
