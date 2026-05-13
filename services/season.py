"""季节卡池：限定 UP + 期间外从全局池剔除。

配置走 _conf_schema.json 的 season_pool（JSON 字符串）：
  {"name": "...", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
   "chars": ["作品!角色.jpg", ...], "rate_up": 0.30}
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime


def _roll(prob: float) -> bool:
    if prob <= 0:
        return False
    if prob >= 1:
        return True
    return secrets.randbelow(1_000_000) < int(prob * 1_000_000)


class SeasonService:
    def __init__(self, raw_config: str, get_today_fn):
        self.get_today_fn = get_today_fn
        self.cfg: dict = {}
        if raw_config:
            try:
                self.cfg = json.loads(raw_config) or {}
            except Exception:
                self.cfg = {}
        self.cfg.setdefault("chars", [])
        self.cfg.setdefault("rate_up", 0.0)

    def is_active(self) -> bool:
        if not self.cfg.get("chars"):
            return False
        s, e = self.cfg.get("start"), self.cfg.get("end")
        if not s or not e:
            return False
        try:
            today = datetime.fromisoformat(self.get_today_fn()).date()
            return datetime.fromisoformat(s).date() <= today <= datetime.fromisoformat(e).date()
        except Exception:
            return False

    def name(self) -> str:
        return str(self.cfg.get("name") or "限定卡池")

    def chars(self) -> list[str]:
        return list(self.cfg.get("chars") or [])

    def filter_pool(self, lines: list[str]) -> list[str]:
        """期间外：从池里剔除限定角色。期间内：原样保留（让正常池也能出，但 UP 提高命中率）。"""
        if self.is_active():
            return lines
        if not self.cfg.get("chars"):
            return lines
        ban = set(self.cfg["chars"])
        return [l for l in lines if l not in ban]

    def roll(self, drawn_pool: dict, gid: str, uid: str) -> str | None:
        if not self.is_active():
            return None
        rate = float(self.cfg.get("rate_up") or 0)
        if rate <= 0:
            return None
        if not _roll(rate):
            return None
        chars = self.chars()
        if not chars:
            return None
        drawn = set(drawn_pool.get(gid, {}).get(uid, []))
        avail = [c for c in chars if c not in drawn]
        if not avail:
            avail = chars
        return secrets.choice(avail)
