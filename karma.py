"""
karma.py — 业力系统
====================
从 astrbot_plugin_animewifex 剥离的独立模块。

功能一：业力惩罚
  - 今日每有人对某用户「重置换」成功一次，该用户积累 1 点业力
  - 用户执行「抽老婆」时触发判定：业力值越高，抽到惩罚图的概率越大
  - 概率 = min(karma_count × base_prob, max_prob)
  - 业力计数每日清零（UTC+8）

功能二：UP 池
  - 抽老婆时以 up_prob 概率强制抽到 up_char 指定角色
  - up_char / up_prob 均写在 config 中

功能三：换老婆锁定
  - 当天抽到 lock_chars 列表中任意角色后，当天禁止换老婆
  - lock_chars 与 up_char 独立配置

功能四：常驻 UP 池
  - 抽老婆时以 up_pool_prob 概率从 up_pool 列表中随机选一张
  - 触发顺序在单角色 UP 之前

随机性：全部使用 secrets 模块（密码学安全随机数），不使用 random。

对外暴露：
  KarmaSystem   核心类，无框架依赖，可单独测试
"""

from __future__ import annotations

import secrets
from typing import Callable

# 概率判定精度：百万分之一
_PRECISION = 1_000_000


def _roll(prob: float) -> bool:
    """使用 secrets 模块进行概率判定。
    
    比 random.random() 使用密码学安全随机源，
    避免伪随机数的周期性规律被利用。
    """
    if prob <= 0:
        return False
    if prob >= 1:
        return True
    return secrets.randbelow(_PRECISION) < int(prob * _PRECISION)


class KarmaSystem:
    """
    业力系统主类。

    Parameters
    ----------
    punishment_imgs : list[str]
        业力惩罚图路径列表（list.txt 中的相对路径）。为空则惩罚功能不生效。
    base_prob : float
        每点业力增加的触发概率，默认 0.15（15%）。
    max_prob : float
        触发概率上限，默认 0.80（80%）。
    up_char : str
        UP 池角色路径（list.txt 中的相对路径）。为空则 UP 池不生效。
    up_prob : float
        UP 池触发概率，默认 0.10（10%）。
    lock_chars : list[str]
        换老婆锁定角色路径列表（list.txt 中的相对路径）。
        当天抽到列表中任意一张后禁止换老婆。为空则锁定功能不生效。
    get_today_fn : Callable[[], str]
        返回当前日期字符串（YYYY-MM-DD）的函数，由外部注入便于测试。
    """

    def __init__(
        self,
        punishment_imgs: list[str],
        base_prob: float = 0.15,
        max_prob: float = 0.80,
        up_char: str = "",
        up_prob: float = 0.10,
        lock_chars: list[str] | None = None,
        up_pool: list[str] | None = None,
        up_pool_prob: float = 0.05,
        get_today_fn: Callable[[], str] | None = None,
    ):
        self.punishment_imgs = [img for img in punishment_imgs if img]
        self.base_prob       = base_prob
        self.max_prob        = max_prob
        self.up_char         = up_char.strip()
        self.up_prob         = up_prob
        self.lock_chars      = [c.strip() for c in (lock_chars or []) if c and c.strip()]
        self.up_pool         = [c.strip() for c in (up_pool or []) if c and c.strip()]
        self.up_pool_prob    = up_pool_prob
        self._get_today      = get_today_fn or self._default_today

    # ------------------------------------------------------------------
    # 功能一：业力惩罚
    # ------------------------------------------------------------------

    def karma_active(self) -> bool:
        """业力惩罚功能是否已配置惩罚图。"""
        return bool(self.punishment_imgs)

    def accumulate(self, karma_store: dict, gid: str, uid: str) -> None:
        """
        累加一次业力（在「重置换」成功时调用）。

        Parameters
        ----------
        karma_store : dict
            持久化字典，格式为 {gid: {uid: {"date": str, "count": int}}}。
            调用方负责读写持久化，此方法直接修改传入的 dict。
        gid : str
            群组 ID。
        uid : str
            被重置换的目标用户 ID（业力累积在他身上）。
        """
        today = self._get_today()
        grp   = karma_store.setdefault(gid, {})
        rec   = grp.get(uid, {"date": today, "count": 0})
        if rec.get("date") != today:
            rec = {"date": today, "count": 0}
        rec["count"] += 1
        grp[uid] = rec

    def roll_karma(self, karma_store: dict, gid: str, uid: str) -> tuple[bool, str | None, int]:
        """
        执行业力惩罚判定（在「抽老婆」时调用，优先于正常抽取）。

        Returns
        -------
        triggered : bool
            是否触发惩罚。
        img_path : str | None
            随机选出的惩罚图路径；未触发时为 None。
        karma_count : int
            当前业力值（用于构造提示文字）。
        """
        if not self.karma_active():
            return False, None, 0

        today       = self._get_today()
        rec         = karma_store.get(gid, {}).get(uid, {})
        karma_count = rec.get("count", 0) if rec.get("date") == today else 0

        if karma_count <= 0:
            return False, None, 0

        prob = min(karma_count * self.base_prob, self.max_prob)
        if _roll(prob):
            chosen = secrets.choice(self.punishment_imgs)
            return True, chosen, karma_count

        return False, None, karma_count

    def calc_prob_pct(self, karma_count: int) -> int:
        """返回当前业力对应的触发概率（整数百分比，供提示文字使用）。"""
        return int(min(karma_count * self.base_prob, self.max_prob) * 100)

    # ------------------------------------------------------------------
    # 功能二：UP 池
    # ------------------------------------------------------------------

    def up_active(self) -> bool:
        """UP 池功能是否已配置。"""
        return bool(self.up_char) and self.up_prob > 0

    def roll_up(self) -> str | None:
        """
        执行 UP 池判定（在「抽老婆」时调用，业力判定之后、正常抽取之前）。

        Returns
        -------
        str | None
            触发时返回 up_char 路径，未触发时返回 None。
        """
        if not self.up_active():
            return None
        if _roll(self.up_prob):
            return self.up_char
        return None


    # ------------------------------------------------------------------
    # 功能四：常驻 UP 池
    # ------------------------------------------------------------------

    def up_pool_active(self) -> bool:
        """常驻 UP 池功能是否已配置。"""
        return bool(self.up_pool) and self.up_pool_prob > 0

    def roll_up_pool(self) -> str | None:
        """
        执行常驻 UP 池判定（在业力判定之后、单角色 UP 判定之前）。

        Returns
        -------
        str | None
            触发时从 up_pool 随机返回一张图路径，未触发时返回 None。
        """
        if not self.up_pool_active():
            return None
        if _roll(self.up_pool_prob):
            return secrets.choice(self.up_pool)
        return None

    # ------------------------------------------------------------------
    # 功能三：换老婆锁定
    # ------------------------------------------------------------------

    def lock_active(self) -> bool:
        """换老婆锁定功能是否已配置。"""
        return bool(self.lock_chars)

    def is_lock_char(self, img: str) -> bool:
        """
        判断抽到的老婆是否为锁定角色（列表中任意一张均算）。

        Parameters
        ----------
        img : str
            当前老婆的图片路径（list.txt 中的相对路径）。
        """
        if not self.lock_active():
            return False
        return img in self.lock_chars

    def check_locked(self, cfg: dict, uid: str, today: str) -> bool:
        """
        判断用户今天是否已触发锁定（即今天抽到了 lock_chars 中的任意角色）。

        Parameters
        ----------
        cfg : dict
            群组配置（格式同 load_group_config 返回值）。
        uid : str
            用户 ID。
        today : str
            当前日期字符串（YYYY-MM-DD）。
        """
        if not self.lock_active():
            return False
        wife_data = cfg.get(uid)
        if not isinstance(wife_data, list) or len(wife_data) < 2:
            return False
        if wife_data[1] != today:
            return False
        return wife_data[0] in self.lock_chars

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _default_today() -> str:
        from datetime import datetime, timedelta
        return (datetime.utcnow() + timedelta(hours=8)).date().isoformat()
