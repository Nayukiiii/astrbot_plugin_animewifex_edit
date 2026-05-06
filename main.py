from astrbot.api.all import *
from astrbot.api.star import StarTools
from datetime import datetime, timedelta
import random
import secrets
import os
import json
import aiohttp
import asyncio
import io
from PIL import Image as PilImage
import re
from urllib.parse import quote
from .hentai_search import HentaiSearcher
from .karma import KarmaSystem, _roll
from .services.character_resolver import CharacterResolver
from .services.github_publisher import GitHubPublisher
from .services.image_fetcher import ImageFetcher
from .services.retention import RetentionService
from .services.review import ReviewStatus
from .services.translation import TranslationCache

# ==================== 常量定义 ====================

PLUGIN_DIR = StarTools.get_data_dir("astrbot_plugin_animewifex")
CONFIG_DIR = os.path.join(PLUGIN_DIR, "config")
IMG_DIR = os.path.join(PLUGIN_DIR, "img", "wife")

# 确保目录存在
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)

# 数据文件路径
RECORDS_FILE = os.path.join(CONFIG_DIR, "records.json")
SWAP_REQUESTS_FILE = os.path.join(CONFIG_DIR, "swap_requests.json")
NTR_STATUS_FILE = os.path.join(CONFIG_DIR, "ntr_status.json")
ADD_SESSIONS_FILE = os.path.join(CONFIG_DIR, "add_sessions.json")
PENDING_FILE = os.path.join(CONFIG_DIR, "pending.json")
EN_CACHE_FILE    = os.path.join(CONFIG_DIR, "en_cache.json")    # 角色名→英文名缓存
KARMA_GROUPS_FILE = os.path.join(CONFIG_DIR, "karma_groups.json") # 分群业力配置

# ==================== 全局数据存储 ====================

records = {  # 统一的记录数据结构
    "ntr": {},        # 牛老婆记录
    "change": {},     # 换老婆记录
    "reset": {},      # 重置使用次数
    "swap": {},       # 交换老婆请求次数
    "karma_resets": {},  # 每日重置换成功次数（用于业力系统）
    "draw_stats": {}  # 抽老婆留存统计 {gid: {uid: {last_date, streak, total_draws}}}
}
drawn_pool      = {}   # 去重池 {gid: {uid: [img, ...]}}
_list_cache_mem: list[str] = []  # list_cache.txt 内存缓存，避免每次抽老婆读盘
DRAWN_POOL_MAX  = 500  # 每人最多保留最近N条，超出滚动丢弃
DRAWN_POOL_FILE = os.path.join(CONFIG_DIR, "drawn_pool.json")  # 持久化文件
ADD_SESSION_TTL = 180  # 添老婆交互有效期，群聊里给手机用户多一点反应时间
_drawn_pool_dirty = False  # 脏标记，有变更才写盘
_karma_cache = {}   # 每群 KarmaSystem 实例缓存 {gid: KarmaSystem}
swap_requests = {}  # 交换请求数据
ntr_statuses = {}   # NTR 开关状态
add_sessions = {}        # 添老婆选角色临时会话 {gid: {uid: {candidates, expire_time}}}
pending_queue = {}       # 待审核队列 {pid: {...}}
admin_img_sessions = {}  # 管理员图片确认会话 {pid: {images, img_dir, expire_time}}

# ==================== 并发锁 ====================

config_locks = {}      # 群组配置锁


def get_config_lock(group_id: str) -> asyncio.Lock:
    """获取或创建群组配置锁"""
    if group_id not in config_locks:
        config_locks[group_id] = asyncio.Lock()
    return config_locks[group_id]

def get_today():
    """获取当前上海时区日期字符串"""
    utc_now = datetime.utcnow()
    return (utc_now + timedelta(hours=8)).date().isoformat()


def load_json(path: str) -> dict:
    """安全加载 JSON 文件"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_json(path: str, data: dict) -> None:
    """保存数据到 JSON 文件"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def load_group_config(group_id: str) -> dict:
    """加载群组配置"""
    return load_json(os.path.join(CONFIG_DIR, f"{group_id}.json"))


def save_group_config(group_id: str, config: dict) -> None:
    """保存群组配置"""
    save_json(os.path.join(CONFIG_DIR, f"{group_id}.json"), config)


def load_ntr_statuses():
    """加载 NTR 开关状态"""
    raw = load_json(NTR_STATUS_FILE)
    ntr_statuses.clear()
    ntr_statuses.update(raw)


def save_ntr_statuses():
    """保存 NTR 开关状态"""
    save_json(NTR_STATUS_FILE, ntr_statuses)


def load_add_sessions():
    """加载添老婆会话，清理过期的"""
    import time
    raw = load_json(ADD_SESSIONS_FILE)
    now = time.time()
    cleaned = {}
    for gid, users in raw.items():
        valid = {uid: s for uid, s in users.items() if s.get("expire_time", 0) > now}
        if valid:
            cleaned[gid] = valid
    add_sessions.clear()
    add_sessions.update(cleaned)


def save_add_sessions():
    """保存添老婆会话"""
    save_json(ADD_SESSIONS_FILE, add_sessions)


def load_pending():
    """加载待审核队列"""
    raw = load_json(PENDING_FILE)
    pending_queue.clear()
    pending_queue.update(raw)


def save_pending():
    """保存待审核队列"""
    save_json(PENDING_FILE, pending_queue)

def load_drawn_pool():
    """加载去重池，每人截断到最近 DRAWN_POOL_MAX 条"""
    raw = load_json(DRAWN_POOL_FILE)
    drawn_pool.clear()
    for gid, users in raw.items():
        drawn_pool[gid] = {
            uid: lst[-DRAWN_POOL_MAX:] for uid, lst in users.items() if lst
        }

def save_drawn_pool():
    """保存去重池到文件"""
    global _drawn_pool_dirty
    save_json(DRAWN_POOL_FILE, drawn_pool)
    _drawn_pool_dirty = False




# ==================== 数据加载和保存函数 ====================

def load_records():
    """加载所有记录数据，清理非今日的旧记录防止无限堆积"""
    raw = load_json(RECORDS_FILE)
    today = get_today()

    def _clean(d: dict) -> dict:
        """只保留今日记录"""
        return {
            gid: {uid: rec for uid, rec in users.items() if rec.get("date") == today}
            for gid, users in d.items()
        }

    records.clear()
    records.update({
        "ntr":          _clean(raw.get("ntr", {})),
        "change":       _clean(raw.get("change", {})),
        "reset":        _clean(raw.get("reset", {})),
        "swap":         _clean(raw.get("swap", {})),
        "karma_resets": _clean(raw.get("karma_resets", {})),
        "draw_stats":   raw.get("draw_stats", {}),
    })


def save_records():
    """保存所有记录数据"""
    save_json(RECORDS_FILE, records)


def load_swap_requests():
    """加载交换请求并清理过期数据"""
    raw = load_json(SWAP_REQUESTS_FILE)
    today = get_today()
    cleaned = {}
    
    for gid, reqs in raw.items():
        valid = {uid: rec for uid, rec in reqs.items() if rec.get("date") == today}
        if valid:
            cleaned[gid] = valid
    
    swap_requests.clear()
    swap_requests.update(cleaned)
    if raw != cleaned:
        save_json(SWAP_REQUESTS_FILE, cleaned)


def save_swap_requests():
    """保存交换请求"""
    save_json(SWAP_REQUESTS_FILE, swap_requests)


# 初始加载所有数据
load_records()
load_swap_requests()
load_ntr_statuses()
load_add_sessions()
load_pending()
load_drawn_pool()

# ==================== 主插件类 ====================


@register(
    "astrbot_plugin_animewifex",
    "monbed",
    "群二次元老婆插件修改版",
    "1.7.3",
    "https://github.com/monbed/astrbot_plugin_animewifex",
)
class WifePlugin(Star):
    """二次元老婆插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._init_config()
        self._init_commands()
        self.admins = self.load_admins()
        self.translation_cache = TranslationCache(EN_CACHE_FILE)
        self.character_resolver = CharacterResolver()
        self._hentai_searcher = HentaiSearcher(
            self.config,
            en_cache_fn=self._get_en_name_from_cache,
            en_cache_write_fn=self._write_en_name_cache_sync,
            trans_cache_fn=self._get_translation_from_cache,
            trans_cache_write_fn=self._write_translation_cache_sync,
        )
        self.image_fetcher = ImageFetcher(self.config, translate_fn=self._ai_translate_multi)
        self.github_publisher = GitHubPublisher(
            self.config,
            list_cache_path=os.path.join(CONFIG_DIR, "list_cache.txt"),
            translation_profile_fn=self._get_translation_from_cache,
        )
        self.retention = RetentionService(
            records,
            drawn_pool,
            list_cache_size_fn=self._list_cache_size,
            save_records_fn=save_records,
            get_today_fn=get_today,
            change_limit=self.change_max_per_day,
            ntr_limit=self.ntr_max,
            swap_limit=self.swap_max_per_day,
        )
        # 启动时异步拉取 list 缓存
        asyncio.create_task(self._refresh_list_cache())
        # 启动时后台静默补全英文名缓存（限速慢跑，不影响正常使用）
        asyncio.create_task(self._bg_fill_en_cache())
        # 定时写盘：每5分钟把去重池脏数据写入文件
        asyncio.create_task(self._bg_flush_drawn_pool())

    def _init_config(self):
        """初始化配置参数"""
        self.need_prefix = self.config.get("need_prefix")
        self.ntr_max = self.config.get("ntr_max")
        self.ntr_possibility = self.config.get("ntr_possibility")
        self.change_max_per_day = self.config.get("change_max_per_day")
        self.swap_max_per_day = self.config.get("swap_max_per_day")
        self.reset_max_uses_per_day = self.config.get("reset_max_uses_per_day")
        self.reset_success_rate = self.config.get("reset_success_rate")
        self.reset_mute_duration = self.config.get("reset_mute_duration")
        self.image_base_url = self.config.get("image_base_url").rstrip("/") + "/"
        self.image_list_url = self.config.get("image_list_url")
        # 添老婆相关配置
        self.github_token  = self.config.get("github_token")
        self.github_repo   = self.config.get("github_repo")
        self.github_branch = self.config.get("github_branch")
        self.admin_qq      = self.config.get("admin_qq")
        self.pixiv_refresh_token = self.config.get("pixiv_refresh_token")
        # eh/nvidia 配置由 HentaiSearcher 自行从 config 读取，此处不再单独存储
        # 业力系统全局默认配置（各群未单独配置时 fallback 到这里）
        self._karma_global_cfg = {
            "punishment_imgs": [
                self.config.get("karma_img1", ""),
                self.config.get("karma_img2", ""),
            ],
            "base_prob" : self.config.get("karma_base_prob", 0.15),
            "max_prob"  : self.config.get("karma_max_prob", 0.80),
            "up_chars"  : [s.strip() for s in self.config.get("up_chars", "").split(",") if s.strip()],
            "up_prob"   : self.config.get("up_prob", 0.10),
            "lock_chars"  : [s.strip() for s in self.config.get("lock_char", "").split(",") if s.strip()],
            "up_pool"       : [],
            "up_pool_prob"  : self.config.get("up_pool_prob", 0.05),
        }
        # 去重池重置角色（全局）
        self.reset_char = self.config.get("reset_char", "")
        # 群组业力缓存清空（重载配置时重建）
        _karma_cache.clear()


    def _get_karma(self, gid: str) -> KarmaSystem:
        """按群返回对应的 KarmaSystem 实例。
        优先读取 karma_groups.json 中的群配置，没有则 fallback 到全局默认配置。
        结果缓存在 _karma_cache 中，重启或重载插件时自动清空重建。

        karma_groups.json 格式（存放于 CONFIG_DIR）：
        {
            "群号": {
                "karma_img1": "来源!角色.jpg",
                "karma_img2": "来源!角色.jpg",
                "base_prob": 0.15,
                "max_prob": 0.80,
                "up_chars": [],
                "up_prob": 0.10,
                "lock_chars": []
            }
        }
        """
        if gid in _karma_cache:
            return _karma_cache[gid]

        group_cfgs = load_json(KARMA_GROUPS_FILE)
        gcfg = group_cfgs.get(gid, {})

        def _get(key, default):
            return gcfg[key] if key in gcfg else self._karma_global_cfg.get(key, default)

        if gcfg:
            punishment_imgs = [
                gcfg.get("karma_img1", ""),
                gcfg.get("karma_img2", ""),
            ]
        else:
            punishment_imgs = self._karma_global_cfg["punishment_imgs"]

        instance = KarmaSystem(
            punishment_imgs = punishment_imgs,
            base_prob   = _get("base_prob",  0.15),
            max_prob    = _get("max_prob",   0.80),
            up_chars    = _get("up_chars",   []),
            up_prob     = _get("up_prob",    0.10),
            lock_chars   = _get("lock_chars",   []),
            up_pool      = _get("up_pool",      []),
            up_pool_prob = _get("up_pool_prob", 0.05),
            get_today_fn = get_today,
        )
        _karma_cache[gid] = instance
        return instance

    def _init_commands(self):
        """初始化命令映射表"""
        self.commands = {
            "老婆帮助": self.wife_help,
            "抽老婆": self.animewife,
            "查老婆": self.search_wife,
            "老婆图鉴": self.wife_album,
            "我的图鉴": self.wife_album,
            "今日老婆榜": self.today_wife_board,
            "连续抽老婆排行": self.draw_streak_rank,
            "老婆排行": self.draw_streak_rank,
            "图鉴排行": self.album_rank,
            "牛老婆": self.ntr_wife,
            "重置牛": self.reset_ntr,
            "切换ntr开关状态": self.switch_ntr,
            "换老婆": self.change_wife,
            "重置换": self.reset_change_wife,
            "交换老婆": self.swap_wife,
            "同意交换": self.agree_swap_wife,
            "拒绝交换": self.reject_swap_wife,
            "查看交换请求": self.view_swap_requests,
            "要本子": self.get_hentai,
            "添老婆": self.add_wife,
            "我的老婆申请": self.my_wife_submissions,
            "补充来源": self.add_wife_source,
            "刷新缓存": self.rebuild_en_cache,
            "解析角色": self.inspect_translation,
            "重译角色": self.retranslate_character,
            "pr上线": self.pr_online,
        }

    def load_admins(self) -> list:
        """加载管理员列表"""
        path = os.path.join("data", "cmd_config.json")
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
                admins = cfg.get("admins_id", [])
                return [str(admin_id) for admin_id in admins]
        except Exception:
            return []

    def parse_at_target(self, event: AstrMessageEvent) -> str | None:
        """解析消息中的@目标用户"""
        if not event.message_obj or not hasattr(event.message_obj, "message"):
            return None
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                return str(comp.qq)
        return None

    def parse_target(self, event: AstrMessageEvent) -> str | None:
        """解析命令目标用户"""
        target = self.parse_at_target(event)
        if target:
            return target
        
        msg = event.message_str.strip()
        if msg.startswith("牛老婆") or msg.startswith("查老婆"):
            parts = msg.split(maxsplit=1)
            if len(parts) > 1:
                name = parts[1]
                group_id = str(event.message_obj.group_id)
                cfg = load_group_config(group_id)
                for uid, data in cfg.items():
                    if isinstance(data, list) and len(data) > 2:
                        if data[2] == name:
                            return uid
        return None

    # ==================== 消息处理 ====================

    @event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def on_private_messages(self, event: AstrMessageEvent, *args, **kwargs):
        """私聊消息处理：管理员审核回复"""
        async for res in self._handle_private_review(event):
            yield res

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_all_messages(self, event: AstrMessageEvent, *args, **kwargs):
        """消息分发处理（仅群聊监听）"""
        logger.info("[debug] unified_msg_origin=%r" % event.unified_msg_origin)
        if not event.message_obj or not hasattr(event.message_obj, "group_id"):
            return

        # 检查是否需要前缀唤醒
        if self.need_prefix and not event.is_at_or_wake_command:
            return

        text = event.message_str.strip()

        # 先检查添老婆会话（waiting_input / waiting_choice）
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        session = add_sessions.get(gid, {}).get(uid)
        if session and session.get("step") == "waiting_choice":
            event.stop_event()
            async for res in self.add_wife(event):
                yield res
            return
        if session and session.get("step") == "waiting_input":
            import time as _t
            if session.get("expire_time", 0) > _t.time():
                orig = session.get("query", "")
                if text == "取消" or "/" in text or text == orig:
                    event.stop_event()
                    async for res in self.add_wife(event):
                        yield res
                    return
            else:
                add_sessions.get(gid, {}).pop(uid, None)
                save_add_sessions()
            # 过期或无关内容：继续正常分发
        if session and session.get("step") == "waiting_manual_source":
            import time as _t
            if session.get("expire_time", 0) > _t.time():
                event.stop_event()
                async for res in self.add_wife(event):
                    yield res
                return
            add_sessions.get(gid, {}).pop(uid, None)
            save_add_sessions()

        # 需要精确匹配的指令（防止类似「换老婆还没弄好吗」误触发）
        _EXACT_CMDS = {"换老婆"}

        for cmd, func in self.commands.items():
            matched = (text == cmd) if cmd in _EXACT_CMDS else text.startswith(cmd)
            if matched:
                event.stop_event()
                async for res in func(event):
                    yield res
                break

    # ==================== 抽老婆相关 ====================

    async def animewife(self, event: AstrMessageEvent):
        """抽老婆"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()

        # ── 今天已抽过：直接返回当天结果，不重复判定 ──
        async with get_config_lock(gid):
            _cfg_today = load_group_config(gid)
            _wd_today  = _cfg_today.get(uid)
            if isinstance(_wd_today, list) and len(_wd_today) >= 2 and _wd_today[1] == today and self._is_valid_img_path(_wd_today[0]):
                self._record_daily_draw(gid, uid, today)
                yield event.chain_result(await self._build_wife_message(_wd_today[0], nick, gid=gid, uid=uid))
                return

        # ── 业力惩罚判定 ──
        karma = self._get_karma(gid)
        triggered, karma_img, karma_count = karma.roll_karma(records["karma_resets"], gid, uid)
        if triggered:
            prob_pct = karma.calc_prob_pct(karma_count)
            # 从惩罚图路径提取角色名，如 img2/来源!角色名.jpg → 角色名
            _karma_char = os.path.splitext(karma_img)[0].split("/")[-1].split("!")[-1] if karma_img else "惩罚角色"
            msg = (
                f"{nick}，业力反噬！你今天让人帮你重置了 {karma_count} 次"
                f"（触发概率 {prob_pct}%），天道好轮回——\n"
                f"今天的老婆是{_karma_char}！💍\n"
                f"专情才是真理，明天从头开始吧~"
            )
            # 写入群组配置，使「查老婆」能正常显示
            async with get_config_lock(gid):
                cfg = load_group_config(gid)
                cfg[uid] = [karma_img, today, nick, "karma_locked"]
                save_group_config(gid, cfg)
            self._record_daily_draw(gid, uid, today)
            msg += self._retention_hint(gid, uid)
            img_comp = await self._resolve_wife_image(karma_img)
            if img_comp:
                yield event.chain_result([Plain(msg), img_comp])
            else:
                yield event.plain_result(msg)
            return

        # ── 常驻 UP 池判定 ──
        pool_img = karma.roll_up_pool()
        if pool_img:
            async with get_config_lock(gid):
                cfg = load_group_config(gid)
                cfg[uid] = [pool_img, today, nick]
                save_group_config(gid, cfg)
            self._record_daily_draw(gid, uid, today)
            _p_char = os.path.splitext(pool_img)[0].split("/")[-1].split("!")[-1] if pool_img else ""
            _p_src  = os.path.splitext(pool_img)[0].split("/")[-1].split("!")[0]  if "!" in pool_img else ""
            _p_text = (
                f"{nick}，[UP] 今天的老婆是来自《{_p_src}》的{_p_char}～"
                if _p_src else
                f"{nick}，[UP] 今天的老婆是{_p_char}～"
            )
            _p_text += self._retention_hint(gid, uid)
            img_comp = await self._resolve_wife_image(pool_img)
            if img_comp:
                yield event.chain_result([Plain(_p_text), img_comp])
            else:
                yield event.plain_result(_p_text)
            return

        # ── 单角色 UP 池判定 ──
        up_img = karma.roll_up()
        if up_img:
            async with get_config_lock(gid):
                cfg = load_group_config(gid)
                cfg[uid] = [up_img, today, nick]
                save_group_config(gid, cfg)
            self._record_daily_draw(gid, uid, today)
            _up_char = os.path.splitext(up_img)[0].split("/")[-1].split("!")[-1] if up_img else ""
            _up_src  = os.path.splitext(up_img)[0].split("/")[-1].split("!")[0]  if "!" in up_img else ""
            _up_text = (
                f"{nick}，[UP] 今天的老婆是来自《{_up_src}》的{_up_char}～"
                if _up_src else
                f"{nick}，[UP] 今天的老婆是{_up_char}～"
            )
            _up_text += self._retention_hint(gid, uid)
            img_comp = await self._resolve_wife_image(up_img)
            if img_comp:
                yield event.chain_result([Plain(_up_text), img_comp])
            else:
                yield event.plain_result(_up_text)
            return

        # 先在锁外判断是否需要重新抽取
        img = None
        need_fetch = False
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            wife_data = cfg.get(uid)
            if not wife_data or not isinstance(wife_data, list) or wife_data[1] != today or not self._is_valid_img_path(wife_data[0]):
                need_fetch = True
            else:
                img = wife_data[0]

        if need_fetch:
            img = await self._fetch_wife_image_for_user(gid, uid)
            if not img:
                yield event.plain_result("抱歉，今天的老婆获取失败了，请稍后再试~")
                return
            async with get_config_lock(gid):
                cfg = load_group_config(gid)
                cfg[uid] = [img, today, nick]
                save_group_config(gid, cfg)
            self._record_daily_draw(gid, uid, today)

        # 生成并发送消息
        yield event.chain_result(await self._build_wife_message(img, nick, gid=gid, uid=uid))

    def _is_valid_img_path(self, img: str) -> bool:
        """校验图片路径是否合法（必须含!且有图片扩展名）"""
        img_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp")
        return bool(img) and "!" in img and img.lower().endswith(img_exts)

    async def _fetch_wife_image(self) -> str | None:
        """获取老婆图片（从 list_cache.txt / 远程 list.txt 随机选取文件名）"""
        img_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp")

        def _filter(lines):
            return [l for l in lines if "!" in l and l.lower().endswith(img_exts)]

        # 优先从本地缓存的 list_cache.txt 中随机选
        try:
            cache_path = os.path.join(CONFIG_DIR, "list_cache.txt")
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    lines = _filter([l.strip() for l in f if l.strip()])
                if lines:
                    return secrets.choice(lines)
        except Exception:
            pass

        # 缓存不存在则直接从远程 list.txt 获取
        try:
            url = self.image_list_url
            if url:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            lines = _filter([l.strip() for l in text.splitlines() if l.strip()])
                            if lines:
                                return secrets.choice(lines)
        except Exception:
            pass

        return None

    async def _fetch_wife_image_for_user(self, gid: str, uid: str) -> str | None:
        """获取老婆图片，排除去重池中已出现的图片。

        逻辑：
        - 优先读内存缓存 _list_cache_mem，避免每次读盘
        - 去重池耗尽时自动重置
        - 抽到 reset_char 时清空去重池，开始新一轮
        """
        img_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp")

        def _filter(lines):
            return [l for l in lines if "!" in l and l.lower().endswith(img_exts)]

        # 优先用内存缓存，避免每次抽老婆读盘
        all_lines = list(_list_cache_mem) if _list_cache_mem else []

        if not all_lines:
            # 内存缓存为空（首次启动未拉取），回落到读本地文件
            try:
                cache_path = os.path.join(CONFIG_DIR, "list_cache.txt")
                if os.path.exists(cache_path):
                    with open(cache_path, "r", encoding="utf-8") as f:
                        all_lines = _filter([l.strip() for l in f if l.strip()])
            except Exception:
                pass

        if not all_lines:
            try:
                url = self.image_list_url
                if url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                text = await resp.text()
                                all_lines = _filter([l.strip() for l in text.splitlines() if l.strip()])
            except Exception:
                pass

        if not all_lines:
            return None

        # 去重池过滤
        drawn = set(drawn_pool.get(gid, {}).get(uid, []))
        available = [l for l in all_lines if l not in drawn]

        # 耗尽时重置（正常情况几千条不会耗尽）
        if not available:
            logger.info(f"[去重池] {gid}/{uid} 已抽完全部图片，自动重置")
            drawn_pool.setdefault(gid, {})[uid] = []
            available = all_lines

        img = secrets.choice(available)

        # 记录到去重池（超出 DRAWN_POOL_MAX 时滚动丢弃最旧的）
        global _drawn_pool_dirty
        pool_list = drawn_pool.setdefault(gid, {}).setdefault(uid, [])
        pool_list.append(img)
        if len(pool_list) > DRAWN_POOL_MAX:
            drawn_pool[gid][uid] = pool_list[-DRAWN_POOL_MAX:]
        _drawn_pool_dirty = True

        # 抽到 reset_char 时清空去重池，开始新一轮
        if self.reset_char and img == self.reset_char:
            logger.info(f"[去重池] {gid}/{uid} 抽到 reset_char，开始新一轮")
            drawn_pool[gid][uid] = []

        return img


    def _record_daily_draw(self, gid: str, uid: str, today: str) -> dict:
        """记录用户每日首次抽取，用于连续天数和累计抽取展示。"""
        return self.retention.record_daily_draw(gid, uid, today)

    def _get_draw_stats(self, gid: str, uid: str) -> dict:
        return self.retention.get_draw_stats(gid, uid)

    def _list_cache_size(self) -> int:
        if _list_cache_mem:
            return len(_list_cache_mem)
        try:
            cache_path = os.path.join(CONFIG_DIR, "list_cache.txt")
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    return sum(
                        1 for line in f
                        if line.strip() and "!" in line and line.strip().lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                    )
        except Exception:
            pass
        return 0

    def _album_summary(self, gid: str, uid: str) -> tuple[int, int, int]:
        return self.retention.album_summary(gid, uid)

    def _remaining_daily_count(self, bucket: str, gid: str, uid: str, limit: int, today: str) -> int:
        return self.retention.remaining_daily_count(bucket, gid, uid, limit, today)

    def _retention_hint(self, gid: str, uid: str) -> str:
        return self.retention.retention_hint(gid, uid)

    async def _build_wife_message(self, img: str, nick: str, gid: str | None = None, uid: str | None = None):
        """构建老婆消息链"""
        name = os.path.splitext(img)[0].split("/")[-1]
        
        if "!" in name:
            source, chara = name.split("!", 1)
            text = f"{nick}，你今天的老婆是来自《{source}》的{chara}，请好好珍惜哦~"
        else:
            text = f"{nick}，你今天的老婆是{name}，请好好珍惜哦~"
        if gid and uid:
            text += self._retention_hint(gid, uid)
        else:
            text += "\n发送「要本子」看看有没有她的本子~"
        
        img_comp = await self._resolve_wife_image(img)
        if img_comp:
            return [Plain(text), img_comp]
        return [Plain(text)]

    async def _resolve_wife_image(self, img: str):
        """解析老婆图片：插件侧下载后转 base64 发送，避免 LLOB 拉 GitHub 超时"""
        import base64 as _b64
        if not self._is_valid_img_path(img):
            logger.warning(f"[老婆图片] 无效路径，跳过: {img!r}")
            return None
        url = self.image_base_url + quote(img, safe="/!")
        logger.info(f"[老婆图片] 请求URL: {url}")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        data = await r.read()
                        try:
                            pil_img = PilImage.open(io.BytesIO(data)).convert("RGB")
                            quality = 85
                            while True:
                                buf = io.BytesIO()
                                pil_img.save(buf, format="JPEG", quality=quality)
                                if buf.tell() <= 400 * 1024 or quality <= 20:
                                    break
                                quality -= 10
                            data = buf.getvalue()
                            logger.info(f"[老婆图片] 压缩后大小: {len(data)/1024:.1f}KB (quality={quality})")
                        except Exception as ce:
                            logger.warning(f"[老婆图片] 压缩失败，使用原图: {ce}")
                        b64 = _b64.b64encode(data).decode()
                        return Image.fromBase64(b64)
                    else:
                        logger.warning(f"[老婆图片] 下载失败: {url} status={r.status}")
        except Exception as e:
            logger.error(f"[老婆图片] 下载异常: {url} {e}")
        return None

    # ==================== 帮助命令 ====================

    async def wife_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """
【基础命令】
• 抽老婆 - 每天抽取一个二次元老婆
• 查老婆 [@用户] - 查看别人的老婆
• 老婆图鉴 - 查看自己的连续天数和图鉴进度
• 今日老婆榜 - 查看本群今天已抽到的老婆
• 老婆排行 - 查看连续抽老婆排行
• 图鉴排行 - 查看本群图鉴进度排行
• 要本子 - AI识别老婆角色及作品，搜索18Comic对应内容

【牛老婆功能】(概率较低😭)
• 牛老婆 [@用户] - 有概率抢走别人的老婆
• 重置牛 [@用户] - 重置牛的次数(失败会禁言)

【换老婆功能】
• 换老婆 - 丢弃当前老婆换新的
• 重置换 [@用户] - 重置换老婆的次数(失败会禁言)

【交换功能】
• 交换老婆 [@用户] - 向别人发起老婆交换请求
• 同意交换 [@发起者] - 同意交换请求
• 拒绝交换 [@发起者] - 拒绝交换请求
• 查看交换请求 - 查看当前的交换请求

【共建老婆库】
• 添老婆 角色名/作品名 - 搜索并提交新角色
• 我的老婆申请 - 查看自己提交的审核进度
• 解析角色 角色名/作品名 - 查看翻译档案与搜索用别名

【管理员命令】
• 切换ntr开关状态 - 开启/关闭NTR功能
• 重译角色 角色名/作品名 - 清除缓存并重新解析角色

💡 提示：部分命令有每日使用次数限制
"""
        yield event.plain_result(help_text.strip())

    async def wife_album(self, event: AstrMessageEvent):
        """查看自己的图鉴与连续抽取进度"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        stats = self._get_draw_stats(gid, uid)
        seen, total, pct = self._album_summary(gid, uid)
        last_date = stats.get("last_date") or "还没有记录"
        lines = [
            f"{nick} 的老婆图鉴",
            f"已见角色：{seen}/{total}（{pct}%）" if total else f"已见角色：{seen}",
            f"连续抽取：{int(stats.get('streak', 0) or 0)} 天",
            f"累计抽取：{int(stats.get('total_draws', 0) or 0)} 天",
            f"最近抽取：{last_date}",
            "",
            "今日入口：抽老婆 / 换老婆 / 交换老婆 @群友 / 添老婆 角色名/作品名",
        ]
        yield event.plain_result("\n".join(lines))

    def _wife_display_name(self, img: str) -> str:
        return self.retention.wife_display_name(img)

    async def today_wife_board(self, event: AstrMessageEvent):
        """查看本群今日老婆榜"""
        gid = str(event.message_obj.group_id)
        today = get_today()
        cfg = load_group_config(gid)
        rows = self.retention.today_wife_rows(cfg, today)

        if not rows:
            yield event.plain_result("今天还没人抽老婆。发「抽老婆」拿下本群第一抽吧~")
            return

        random.shuffle(rows)
        lines = [f"今日老婆榜（{len(rows)} 人已抽）"]
        for i, (nick, wife_name) in enumerate(rows[:15], 1):
            lines.append(f"{i}. {nick}：{wife_name}")
        if len(rows) > 15:
            lines.append(f"...还有 {len(rows) - 15} 人")
        yield event.plain_result("\n".join(lines))

    async def draw_streak_rank(self, event: AstrMessageEvent):
        """查看本群连续抽老婆排行"""
        gid = str(event.message_obj.group_id)
        cfg = load_group_config(gid)
        rows = self.retention.draw_streak_rank_rows(gid, cfg, limit=10)

        if not rows:
            yield event.plain_result("本群还没有连续抽取记录。今天开始养榜吧，发「抽老婆」就行~")
            return

        lines = ["连续抽老婆排行"]
        for i, (streak, total, nick) in enumerate(rows, 1):
            lines.append(f"{i}. {nick}：连续 {streak} 天，累计 {total} 天")
        yield event.plain_result("\n".join(lines))

    async def album_rank(self, event: AstrMessageEvent):
        """查看本群图鉴进度排行"""
        gid = str(event.message_obj.group_id)
        cfg = load_group_config(gid)
        rows, total = self.retention.album_rank_rows(gid, cfg, limit=10)

        if not rows:
            yield event.plain_result("本群还没有图鉴记录。发「抽老婆」开图鉴吧~")
            return

        title = f"图鉴排行（总池 {total} 位）" if total else "图鉴排行"
        lines = [title]
        for i, (seen, pct, nick) in enumerate(rows, 1):
            suffix = f"（{pct}%）" if total else ""
            lines.append(f"{i}. {nick}：已见 {seen} 位{suffix}")
        yield event.plain_result("\n".join(lines))

    async def search_wife(self, event: AstrMessageEvent):
        """查老婆"""
        gid = str(event.message_obj.group_id)
        tid = self.parse_target(event) or str(event.get_sender_id())
        today = get_today()
        
        cfg = load_group_config(gid)
        wife_data = cfg.get(tid)
        
        if not wife_data or not isinstance(wife_data, list) or wife_data[1] != today:
            yield event.plain_result("没有发现老婆的踪迹，快去抽一个试试吧~")
            return
        
        img, _, owner = wife_data[0], wife_data[1], wife_data[2]
        
        name = os.path.splitext(img)[0].split("/")[-1]
        
        if "!" in name:
            source, chara = name.split("!", 1)
            text = f"{owner}的老婆是来自《{source}》的{chara}，羡慕吗？"
        else:
            text = f"{owner}的老婆是{name}，羡慕吗？"
        
        path = os.path.join(IMG_DIR, img)
        try:
            img_comp = await self._resolve_wife_image(img)
            if img_comp:
                yield event.chain_result([Plain(text), img_comp])
            else:
                yield event.plain_result(text)
        except Exception:
            yield event.plain_result(text)

    # ==================== 牛老婆相关 ====================

    async def ntr_wife(self, event: AstrMessageEvent):
        """牛老婆"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        
        # 检查 NTR 功能是否启用
        if not ntr_statuses.get(gid, True):
            yield event.plain_result("牛老婆功能还没开启哦，请联系管理员开启~")
            return
        
        today = get_today()
        
        grp = records["ntr"].setdefault(gid, {})
        rec = grp.get(uid, {"date": today, "count": 0})
        
        if rec["date"] != today:
            rec = {"date": today, "count": 0}
        
        if rec["count"] >= self.ntr_max:
            yield event.plain_result(f"{nick}，你今天已经牛了{self.ntr_max}次啦，明天再来吧~")
            return
        
        # 获取目标用户
        tid = self.parse_target(event)
        if not tid or tid == uid:
            msg = "请@你想牛的对象，或输入完整的昵称哦~" if not tid else "不能牛自己呀，换个人试试吧~"
            yield event.plain_result(f"{nick}，{msg}")
            return
        
        # 检查目标是否有老婆并执行牛操作
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            if tid not in cfg or cfg[tid][1] != today:
                yield event.plain_result("对方今天还没有老婆可牛哦~")
                return
            # 业力锁：目标受业力庇护，无法被牛
            if len(cfg[tid]) > 3 and cfg[tid][3] == "karma_locked":
                _t_name = cfg[tid][2] if len(cfg[tid]) > 2 else "对方"
                yield event.plain_result(f"{_t_name} 的老婆受业力庇护，今天牛不走！")
                return
            
            # 更新牛的次数
            rec["count"] += 1
            grp[uid] = rec
            save_records()
            
            # 判断牛老婆是否成功
            if _roll(self.ntr_possibility):
                # 牛成功：目标用户的老婆转给牛者
                img = cfg[tid][0]
                cfg[uid] = [img, today, nick]
                del cfg[tid]
                save_group_config(gid, cfg)
                
                # 取消相关交换请求
                cancel_msg = self.cancel_swap_on_wife_change(gid, [uid, tid])
                
                yield event.plain_result(f"{nick}，牛老婆成功！老婆已归你所有，恭喜恭喜~")
                if cancel_msg:
                    yield event.plain_result(cancel_msg)
                
                # 直接展示抢到的老婆
                yield event.chain_result(await self._build_wife_message(img, nick))
            else:
                rem = self.ntr_max - rec["count"]
                yield event.plain_result(f"{nick}，很遗憾，牛失败了！你今天还可以再试{rem}次~")

    async def switch_ntr(self, event: AstrMessageEvent):
        """切换 NTR 开关（仅管理员）"""
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        
        if uid not in self.admins:
            yield event.plain_result(f"{nick}，你没有权限操作哦~")
            return
        
        gid = str(event.message_obj.group_id)
        current_status = ntr_statuses.get(gid, True)
        ntr_statuses[gid] = not current_status
        save_ntr_statuses()
        
        state = "开启" if not current_status else "关闭"
        yield event.plain_result(f"{nick}，NTR已{state}")

    # ==================== 换老婆相关 ====================

    async def change_wife(self, event: AstrMessageEvent):
        """换老婆"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        
        # 检查每日换老婆次数
        recs = records["change"].setdefault(gid, {})
        rec = recs.get(uid, {"date": "", "count": 0})
        
        if rec["date"] == today and rec["count"] >= self.change_max_per_day:
            yield event.plain_result(f"{nick}，你今天已经换了{self.change_max_per_day}次老婆啦，明天再来吧~")
            return

        # ── 业力锁 & 换老婆锁定检查（共用一次读取）──
        _cfg_lock = load_group_config(gid)
        _wd_karma = _cfg_lock.get(uid)
        if isinstance(_wd_karma, list) and len(_wd_karma) > 3 and _wd_karma[3] == "karma_locked" and _wd_karma[1] == today:
            _karma_char2 = os.path.splitext(_wd_karma[0])[0].split("/")[-1].split("!")[-1] if _wd_karma[0] else "她"
            yield event.plain_result(f"{nick}，业力缠身，今天就安心陪着{_karma_char2}吧，明天再说~")
            return

        karma = self._get_karma(gid)
        if karma.lock_active():
            if karma.check_locked(_cfg_lock, uid, today):
                # 从今天实际抽到的图路径中提取角色名，避免多锁定角色时提示名字错误
                _actual_img = (_cfg_lock.get(uid) or [""])[0]
                char_hint = os.path.splitext(_actual_img)[0].split("/")[-1].split("!")[-1] if _actual_img else "指定角色"
                yield event.plain_result(
                    f"{nick}，今天已经抽到了{char_hint}，不准换老婆！好好珍惜吧~"
                )
                return

        # 检查是否有老婆并删除
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            if uid not in cfg or cfg[uid][1] != today:
                yield event.plain_result(f"{nick}，你今天还没有老婆，先去抽一个再来换吧~")
                return
            
            # 删除老婆
            del cfg[uid]
            save_group_config(gid, cfg)
        
        # 更新记录
        if rec["date"] != today:
            rec = {"date": today, "count": 1}
        else:
            rec["count"] += 1
        recs[uid] = rec
        save_records()
        
        # 取消相关交换请求
        cancel_msg = self.cancel_swap_on_wife_change(gid, [uid])
        if cancel_msg:
            yield event.plain_result(cancel_msg)
        
        # 立即展示新老婆
        async for res in self.animewife(event):
            yield res

    # ==================== 重置相关 ====================

    async def reset_ntr(self, event: AstrMessageEvent):
        """重置牛老婆次数"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        
        # 管理员可直接重置他人
        if uid in self.admins:
            tid = self.parse_at_target(event) or uid
            if gid in records["ntr"] and tid in records["ntr"][gid]:
                del records["ntr"][gid][tid]
                save_records()
            yield event.chain_result([
                Plain("管理员操作：已重置"), At(qq=int(tid)), Plain("的牛老婆次数。")
            ])
            return
        
        # 普通用户使用重置机会
        grp = records["reset"].setdefault(gid, {})
        rec = grp.get(uid, {"date": today, "count": 0})
        
        if rec.get("date") != today:
            rec = {"date": today, "count": 0}
        
        if rec["count"] >= self.reset_max_uses_per_day:
            yield event.plain_result(f"{nick}，你今天已经用完{self.reset_max_uses_per_day}次重置机会啦，明天再来吧~")
            return
        
        rec["count"] += 1
        grp[uid] = rec
        save_records()
        
        tid = self.parse_at_target(event) or uid
        
        if _roll(self.reset_success_rate):
            if gid in records["ntr"] and tid in records["ntr"][gid]:
                del records["ntr"][gid][tid]
                save_records()
            yield event.chain_result([
                Plain("已重置"), At(qq=int(tid)), Plain("的牛老婆次数。")
            ])
        else:
            try:
                await event.bot.set_group_ban(group_id=int(gid), user_id=int(uid), duration=self.reset_mute_duration)
            except Exception:
                pass
            yield event.plain_result(f"{nick}，重置牛失败，被禁言{self.reset_mute_duration}秒，下次记得再接再厉哦~")

    async def reset_change_wife(self, event: AstrMessageEvent):
        """重置换老婆次数"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        
        # 管理员可直接重置他人
        if uid in self.admins:
            tid = self.parse_at_target(event) or uid
            grp = records["change"].setdefault(gid, {})
            if tid in grp:
                del grp[tid]
                save_records()
            yield event.chain_result([
                Plain("管理员操作：已重置"), At(qq=int(tid)), Plain("的换老婆次数。")
            ])
            return
        
        # 普通用户使用重置机会
        grp = records["reset"].setdefault(gid, {})
        rec = grp.get(uid, {"date": today, "count": 0})
        
        if rec.get("date") != today:
            rec = {"date": today, "count": 0}
        
        if rec["count"] >= self.reset_max_uses_per_day:
            yield event.plain_result(f"{nick}，你今天已经用完{self.reset_max_uses_per_day}次重置机会啦，明天再来吧~")
            return
        
        rec["count"] += 1
        grp[uid] = rec
        save_records()
        
        tid = self.parse_at_target(event) or uid
        
        if _roll(self.reset_success_rate):
            grp2 = records["change"].setdefault(gid, {})
            if tid in grp2:
                # 重置换成功，累计被重置人的业力
                self._get_karma(gid).accumulate(records["karma_resets"], gid, tid)
                del grp2[tid]
                save_records()
            yield event.chain_result([
                Plain("已重置"), At(qq=int(tid)), Plain("的换老婆次数。")
            ])
        else:
            try:
                await event.bot.set_group_ban(group_id=int(gid), user_id=int(uid), duration=self.reset_mute_duration)
            except Exception:
                pass
            yield event.plain_result(f"{nick}，重置换失败，被禁言{self.reset_mute_duration}秒，下次记得再接再厉哦~")

    # ==================== 交换老婆相关 ====================

    async def swap_wife(self, event: AstrMessageEvent):
        """发起交换老婆请求"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        tid = self.parse_at_target(event)
        nick = event.get_sender_name()
        today = get_today()
        
        # 检查每日交换请求次数
        grp_limit = records["swap"].setdefault(gid, {})
        rec_lim = grp_limit.get(uid, {"date": "", "count": 0})
        
        if rec_lim["date"] != today:
            rec_lim = {"date": today, "count": 0}
        
        if rec_lim["count"] >= self.swap_max_per_day:
            yield event.plain_result(f"{nick}，你今天已经发起了{self.swap_max_per_day}次交换请求啦，明天再来吧~")
            return
        
        if not tid or tid == uid:
            yield event.plain_result(f"{nick}，请在命令后@你想交换的对象哦~")
            return
        
        # 检查双方是否都有老婆
        cfg = load_group_config(gid)
        for x in (uid, tid):
            if x not in cfg or cfg[x][1] != today:
                who = nick if x == uid else "对方"
                yield event.plain_result(f"{who}，今天还没有老婆，无法进行交换哦~")
                return
        
        # 记录交换请求
        rec_lim["count"] += 1
        grp_limit[uid] = rec_lim
        save_records()
        
        grp = swap_requests.setdefault(gid, {})
        grp[uid] = {"target": tid, "date": today}
        save_swap_requests()
        
        yield event.chain_result([
            Plain(f"{nick} 想和 "), At(qq=int(tid)),
            Plain(" 交换老婆啦！请对方用\"同意交换 @发起者\"或\"拒绝交换 @发起者\"来回应~")
        ])

    async def agree_swap_wife(self, event: AstrMessageEvent):
        """同意交换老婆"""
        gid = str(event.message_obj.group_id)
        tid = str(event.get_sender_id())
        uid = self.parse_at_target(event)
        nick = event.get_sender_name()
        today = get_today()
        
        grp = swap_requests.get(gid, {})
        rec = grp.get(uid)
        
        if not rec or rec.get("target") != tid:
            yield event.plain_result(f"{nick}，请在命令后@发起者，或用\"查看交换请求\"命令查看当前请求哦~")
            return
        
        # 业力锁检查：双方任一有锁则拦截（先读完再 yield，避免持锁 yield）
        _karma_block_name = None
        cfg_swap = load_group_config(gid)
        for _xid in (uid, tid):
            _xwd = cfg_swap.get(_xid)
            if isinstance(_xwd, list) and len(_xwd) > 3 and _xwd[3] == "karma_locked" and _xwd[1] == today:
                _karma_block_name = _xwd[2] if len(_xwd) > 2 else "其中一方"
                break
        if _karma_block_name:
            grp[uid] = rec  # 还原请求
            yield event.plain_result(f"{_karma_block_name} 业力缠身，无法交换老婆！")
            return

        # 删除请求
        del grp[uid]

        # 执行交换
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            cfg[uid][0], cfg[tid][0] = cfg[tid][0], cfg[uid][0]
            save_group_config(gid, cfg)
        
        # 保存交换请求删除
        save_swap_requests()
        
        # 取消相关交换请求
        cancel_msg = self.cancel_swap_on_wife_change(gid, [uid, tid])
        
        yield event.plain_result("交换成功！你们的老婆已经互换啦，祝幸福~")
        if cancel_msg:
            yield event.plain_result(cancel_msg)

    async def reject_swap_wife(self, event: AstrMessageEvent):
        """拒绝交换老婆"""
        gid = str(event.message_obj.group_id)
        tid = str(event.get_sender_id())
        uid = self.parse_at_target(event)
        nick = event.get_sender_name()
        
        grp = swap_requests.get(gid, {})
        rec = grp.get(uid)
        
        if not rec or rec.get("target") != tid:
            yield event.plain_result(f"{nick}，请在命令后@发起者，或用\"查看交换请求\"命令查看当前请求哦~")
            return
        
        del grp[uid]
        save_swap_requests()
        
        yield event.chain_result([
            At(qq=int(uid)), Plain("，对方婉拒了你的交换请求，下次加油吧~")
        ])

    async def view_swap_requests(self, event: AstrMessageEvent):
        """查看当前交换请求"""
        gid = str(event.message_obj.group_id)
        me = str(event.get_sender_id())
        
        grp = swap_requests.get(gid, {})
        cfg = load_group_config(gid)
        
        # 获取发起的和收到的请求
        my_req = grp.get(me)
        sent_targets = [my_req["target"]] if my_req else []
        received_from = [uid for uid, rec in grp.items() if rec.get("target") == me]
        
        if not sent_targets and not received_from:
            yield event.plain_result("你当前没有任何交换请求哦~")
            return
        
        parts = []
        for tid in sent_targets:
            name = cfg.get(tid, [None, None, "未知用户"])[2]
            parts.append(f"→ 你发起给 {name} 的交换请求")
        
        for uid in received_from:
            name = cfg.get(uid, [None, None, "未知用户"])[2]
            parts.append(f"→ {name} 发起给你的交换请求")
        
        text = "当前交换请求如下：\n" + "\n".join(parts) + "\n请在\"同意交换\"或\"拒绝交换\"命令后@发起者进行操作~"
        yield event.plain_result(text)

    # ==================== 辅助方法 ====================

    def cancel_swap_on_wife_change(self, gid: str, user_ids: list) -> str | None:
        """检查并取消与指定用户相关的交换请求"""
        today = get_today()
        grp = swap_requests.get(gid, {})
        grp_limit = records["swap"].setdefault(gid, {})
        
        # 找出需要取消的交换请求
        to_cancel = [
            req_uid for req_uid, req in grp.items()
            if req_uid in user_ids or req.get("target") in user_ids
        ]
        
        if not to_cancel:
            return None
        
        # 取消请求并返还次数
        for req_uid in to_cancel:
            rec_lim = grp_limit.get(req_uid, {"date": "", "count": 0})
            if rec_lim.get("date") == today and rec_lim.get("count", 0) > 0:
                rec_lim["count"] = max(0, rec_lim["count"] - 1)
                grp_limit[req_uid] = rec_lim
            del grp[req_uid]
        
        save_swap_requests()
        save_records()
        
        return f"已自动取消 {len(to_cancel)} 条相关的交换请求并返还次数~"

    # ==================== 要本子功能（委托给 HentaiSearcher）====================

    async def get_hentai(self, event: AstrMessageEvent):
        """AI 识别老婆角色 + 同时搜索 JM / NH / EH / DL"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        today = get_today()

        cfg = load_group_config(gid)
        wife_data = cfg.get(uid)

        if not wife_data or not isinstance(wife_data, list) or wife_data[1] != today:
            yield event.plain_result("没老婆看什么本子？先去抽一个吧~")
            return

        # 从文件名拆出作品名和角色名
        raw = os.path.splitext(wife_data[0])[0].split("/")[-1]
        if "!" in raw:
            source_name, char_name = raw.split("!", 1)
        else:
            source_name, char_name = "", raw

        display = f"《{source_name}》{char_name}" if source_name else char_name
        yield event.plain_result(f"正在以「{display}」搜索本子，请稍候...")

        result = await self._hentai_searcher.search(char=char_name, source=source_name)
        yield event.plain_result(result.format_text())

    # ==================== AI 翻译（代理到 HentaiSearcher）====================

    async def _ai_translate_multi(self, char: str, source: str = "") -> dict:
        """委托给 HentaiSearcher，供添老婆等功能复用"""
        return await self._hentai_searcher._ai_translate_multi(char=char, source=source)

    def _parse_char_source_arg(self, msg: str, cmd: str) -> tuple[str, str] | None:
        parts = msg.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return None
        raw = parts[1].strip()
        if "/" in raw:
            char, source = raw.split("/", 1)
            return char.strip(), source.strip()
        return raw, ""

    async def inspect_translation(self, event: AstrMessageEvent):
        """查看角色翻译档案，方便定位翻译/拉图问题。"""
        parsed = self._parse_char_source_arg(event.message_str.strip(), "解析角色")
        if not parsed or not parsed[0]:
            yield event.plain_result("用法：解析角色 角色名/作品名\n例如：解析角色 紫苑/eden*")
            return
        char, source = parsed
        cached = self._get_translation_from_cache(char, source)
        trans = cached or await self._ai_translate_multi(char=char, source=source)
        if not trans:
            yield event.plain_result("没有解析结果：未配置 nvidia_api_key，且缓存里也没有这条角色。")
            return

        alt = "、".join(trans.get("alt_char") or []) or "无"
        src = f"《{source}》" if source else ""
        from_cache = "是" if cached else "否，本次已尝试写入缓存"
        lines = [
            f"角色解析：{src}{char}",
            f"缓存命中：{from_cache}",
            f"中文名：{trans.get('zh_char') or '未知'}",
            f"英文/罗马字：{trans.get('en_char') or '未知'}",
            f"日文名：{trans.get('ja_char') or '未知'}",
            f"假名：{trans.get('kana_char') or '未知'}",
            f"别名：{alt}",
            f"英文作品名：{trans.get('en_source') or '未知'}",
            f"日文作品名：{trans.get('ja_source') or '未知'}",
            f"作品短名：{trans.get('short_source') or '无'}",
            f"VTuber：{'是' if trans.get('is_vtuber') else '否'}",
        ]
        yield event.plain_result("\n".join(lines))

    async def retranslate_character(self, event: AstrMessageEvent):
        """管理员命令：清除某个角色翻译缓存并重新解析。"""
        uid = str(event.get_sender_id())
        if uid not in self.admins:
            yield event.plain_result("只有管理员才能重译角色哦~")
            return
        parsed = self._parse_char_source_arg(event.message_str.strip(), "重译角色")
        if not parsed or not parsed[0]:
            yield event.plain_result("用法：重译角色 角色名/作品名\n例如：重译角色 紫苑/eden*")
            return
        char, source = parsed
        removed = self.translation_cache.remove(char, source)
        trans = await self._ai_translate_multi(char=char, source=source)
        if not trans:
            yield event.plain_result(f"已清除 {removed} 条缓存，但重新解析失败。请检查 nvidia_api_key 或稍后再试。")
            return
        yield event.plain_result(
            f"已清除 {removed} 条缓存并重新解析：\n"
            f"英文/罗马字：{trans.get('en_char') or '未知'}\n"
            f"日文名：{trans.get('ja_char') or '未知'}\n"
            f"别名：{'、'.join(trans.get('alt_char') or []) or '无'}\n"
            f"作品短名：{trans.get('short_source') or '无'}"
        )

    # ==================== 添老婆相关 ====================

    async def add_wife(self, event: AstrMessageEvent):
        """添老婆 - 搜索展示3个候选+小图，用户选择后提交审核"""
        import time
        logger.info("[添老婆] add_wife 入口, msg=%r" % event.message_str)
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        msg = event.message_str.strip()
        session = add_sessions.get(gid, {}).get(uid)

        # ── 等待选择阶段：用户回复 1/2/3、「换一批」或「取消」──────
        if session and session.get("step") == "waiting_choice":
            if session.get("expire_time", 0) <= time.time():
                add_sessions[gid].pop(uid, None)
                save_add_sessions()
                return

            if msg == "取消":
                add_sessions[gid].pop(uid, None)
                save_add_sessions()
                yield event.plain_result("已取消~")
                return

            if msg == "换一批":
                query = session.get("query", "")
                offset = session.get("offset", 0) + 3
                saved_hint_source = session.get("hint_source", "")
                add_sessions[gid].pop(uid, None)
                save_add_sessions()
                async for res in self._do_search_and_show(
                    event, gid, uid, nick, query, offset, _hint_source=saved_hint_source
                ):
                    yield res
                return

            candidates = session.get("candidates", [])
            if msg.isdigit():
                idx = int(msg) - 1
                if not (0 <= idx < len(candidates)):
                    yield event.plain_result(f"请输入 1~{len(candidates)} 的数字哦~")
                    return
                chosen = candidates[idx]
            else:
                chosen = next((c for c in candidates if msg.strip() == c.get("name")), None)
                if not chosen:
                    return  # 不是数字、角色名也不是指令，忽略

            # 用用户输入的 hint_source 覆盖数据库返回的 source
            hint_source = session.get("hint_source", "")
            if hint_source:
                chosen = dict(chosen)
                chosen["source"] = hint_source
            add_sessions[gid].pop(uid, None)
            save_add_sessions()

            src = f"《{chosen['source']}》" if chosen.get('source') else ""
            # 最终选定时做一次完整模糊查重
            hit = await self._char_exists_in_list(chosen["name"], source=chosen.get("source", ""))
            if hit:
                dup_name = hit if hit != chosen["name"] else chosen["name"]
                yield event.plain_result(f"「{src}{chosen['name']}」已经在老婆库里了哦~（与「{dup_name}」重复）")
                return

            await self._submit_pending(gid, uid, nick, chosen, umo=event.unified_msg_origin)
            yield event.plain_result(f"已提交「{src}{chosen['name']}」，等待管理员审核~")
            return

        # ── 等待补充作品名阶段 ──────────────────────────────────────────
        if session and session.get("step") == "waiting_input":
            if session.get("expire_time", 0) <= time.time():
                add_sessions[gid].pop(uid, None)
                save_add_sessions()
                return
            if msg == "取消":
                add_sessions[gid].pop(uid, None)
                save_add_sessions()
                yield event.plain_result("已取消~")
                return
            orig_query = session.get("query", "")
            # 用户发了 角色名/作品名 → 带 source 搜
            if "/" in msg:
                char_part, source_part = msg.split("/", 1)
                next_query = char_part.strip() or orig_query
                hint_source = source_part.strip()
            else:
                # 用户直接再发一次角色名确认 → 无 source 搜索；其他内容忽略
                if msg.strip() != orig_query:
                    return  # 不是角色名也不是指令，忽略
                next_query = orig_query
                hint_source = ""
            add_sessions[gid].pop(uid, None)
            save_add_sessions()
            async for res in self._do_search_and_show(event, gid, uid, nick, next_query, offset=0, _hint_source=hint_source):
                yield res
            return

        # ── 数据库搜不到时：等待用户补作品名，直接送人工审核 ───────────────
        if session and session.get("step") == "waiting_manual_source":
            if session.get("expire_time", 0) <= time.time():
                add_sessions[gid].pop(uid, None)
                save_add_sessions()
                return
            if msg == "取消":
                add_sessions[gid].pop(uid, None)
                save_add_sessions()
                yield event.plain_result("已取消~")
                return
            source = msg.strip()
            query = session.get("query", "").strip()
            if not source:
                yield event.plain_result("作品名不能为空哦，回复作品名即可；不想提交就回复「取消」。")
                return
            add_sessions[gid].pop(uid, None)
            save_add_sessions()
            virtual = {
                "name": query,
                "source": source,
                "thumb_url": "",
                "manual_reason": "角色搜索无结果，用户补作品名后送审",
            }
            hit = await self._char_exists_in_list(query, source=source)
            if hit:
                yield event.plain_result(f"「{query}」已经在老婆库里了哦~（与「{hit}」重复）")
                return
            await self._submit_pending(gid, uid, nick, virtual, umo=event.unified_msg_origin)
            yield event.plain_result(f"已按人工方式提交「《{source}》{query}」，等待管理员审核~")
            return

        # ── 入口：解析关键词 ─────────────────────────────────────────
        parts = msg.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result(
                f"{nick}，请用「添老婆 角色名/作品名」来搜索，例如：\n"
                f"添老婆 紫苑/eden*\n"
                f"添老婆 博丽灵梦/东方Project"
            )
            return

        raw = parts[1].strip()

        # 没有斜杠 → 提醒用户补作品名，等待他确认或补充
        if "/" not in raw:
            import time as _time
            add_sessions.setdefault(gid, {})[uid] = {
                "step": "waiting_input",
                "query": raw,
                "source": "",
                "expire_time": _time.time() + ADD_SESSION_TTL,
            }
            save_add_sessions()
            yield event.plain_result(
                f"{nick}，建议加上作品名让搜索更准确，格式：\n"
                f"添老婆 {raw}/作品名\n\n"
                f"如果不知道作品名，直接再发一次「{raw}」继续搜索也可以~"
            )
            return

        # 有斜杠 → 拆分角色名/作品名
        char_part, source_part = raw.split("/", 1)
        query = char_part.strip()
        hint_source = source_part.strip()
        if not query:
            yield event.plain_result(f"{nick}，角色名不能为空哦~")
            return

        async for res in self._do_search_and_show(event, gid, uid, nick, query, offset=0, _hint_source=hint_source):
            yield res

    async def _do_search_and_show(
        self, event: AstrMessageEvent,
        gid: str, uid: str, nick: str,
        query: str, offset: int = 0,
        _hint_source: str = "",            # 用户输入的作品名提示
    ):
        """搜索角色，展示最多3个候选（含小图），进入 waiting_choice 阶段。"""
        import time
        src_hint = f"/《{_hint_source}》" if _hint_source else ""
        yield event.plain_result(f"正在搜索「{query}{src_hint}」，请稍候...")
        logger.info("[添老婆] 搜索 %r offset=%d" % (query, offset))

        # ── Step 1：入口查重（精确+模糊）──────────────────────────────
        if offset == 0:
            hit = await self._char_exists_in_list(query, source=_hint_source)
            if hit:
                dup_name = hit if hit != query else query
                yield event.plain_result(
                    f"「{dup_name}」已经在老婆库里了哦，不需要重复添加~"
                )
                return

        # ── Step 2：搜索角色 ────────────────────────────────────────────
        all_candidates = await self.character_resolver.search_female_characters(
            query, limit=offset + 9, source=_hint_source
        )

        # ── Step 3：过滤已有角色（精确匹配）──
        existing_pairs = self._load_existing_chars()
        existing_char_names = {c for _, c in existing_pairs}
        filtered = []
        for c in all_candidates:
            if c["name"] in existing_char_names:
                logger.info("[添老婆] 候选过滤（精确）: %r" % c["name"])
                continue
            # 候选名与 query 相同 → 已在 Step 1 查过，直接放行（不重复翻译）
            if c["name"] == query:
                filtered.append(c)
                continue
            # 其他候选：只做精确匹配，不再调翻译 API（避免大量API调用）
            # 模糊查重只在用户最终选定时做（见 waiting_choice 分支）
            filtered.append(c)

        page = filtered[offset: offset + 3]

        # ── Step 4：有 hint_source 时过滤 source 不匹配的候选 ──────────
        if _hint_source and offset == 0:
            hint_low = _hint_source.strip().lower()
            def _src_hint_match(c: dict) -> bool:
                s = (c.get("source") or "").strip().lower()
                return bool(s and (hint_low in s or s in hint_low))
            matched = [c for c in filtered if _src_hint_match(c)]
            if matched:
                page = matched[:3]
            else:
                # 搜不到匹配候选 → 直接用用户输入构造虚拟候选提交
                virtual = {"name": query, "source": _hint_source, "thumb_url": ""}
                hit = await self._char_exists_in_list(query, source=_hint_source)
                if hit:
                    yield event.plain_result(f"「{query}」已经在老婆库里了哦~")
                    return
                await self._submit_pending(gid, uid, nick, virtual, umo=event.unified_msg_origin)
                yield event.plain_result(
                    f"未在数据库找到《{_hint_source}》的「{query}」，\n"
                    f"已直接提交审核，等待管理员处理~"
                )
                return

        if not page:
            if offset == 0:
                add_sessions.setdefault(gid, {})[uid] = {
                    "step": "waiting_manual_source",
                    "query": query,
                    "expire_time": time.time() + ADD_SESSION_TTL,
                }
                save_add_sessions()
                yield event.plain_result(
                    f"没找到「{query}」相关角色，换个关键词试试？\n"
                    f"也可以直接回复作品名送人工审核（{ADD_SESSION_TTL}秒内有效）。\n"
                    f"提示：用日文名或英文名搜索效果通常更好。"
                )
            else:
                yield event.plain_result("没有更多候选了，换个关键词试试吧~")
            return

        # 发文字列表
        lines = [f"找到以下角色，回复数字或角色名选择（{ADD_SESSION_TTL}秒内有效）："]
        for i, c in enumerate(page, 1):
            display_src = c.get('source') or _hint_source
            src = f"《{display_src}》" if display_src else "《来源不明》"
            lines.append(f"{i}. {src}{c['name']}")
        has_more = len(filtered) > offset + 3
        lines.append("\n回复「换一批」换下一页" if has_more else "")
        lines.append("回复「取消」退出")
        yield event.plain_result("\n".join(l for l in lines if l))

        # 每个候选发一张小图（有图才发）
        for i, c in enumerate(page, 1):
            if c.get("thumb_url"):
                try:
                    yield event.chain_result([Plain(f"{i}. {c['name']}  "), Image.fromURL(c["thumb_url"])])
                except Exception:
                    pass

        # 保存会话
        add_sessions.setdefault(gid, {})[uid] = {
            "step": "waiting_choice",
            "query": query,
            "offset": offset,
            "candidates": page,
            "hint_source": _hint_source,
            "expire_time": time.time() + ADD_SESSION_TTL,
        }
        save_add_sessions()

    async def add_wife_source(self, event: AstrMessageEvent):
        """补充来源 作品名 —— 补充最近一次提交的作品来源"""
        uid = str(event.get_sender_id())
        gid = str(event.get_group_id())
        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result("用法：补充来源 作品名\n例如：补充来源 世界计划 彩色舞台feat.初音未来")
            return
        source = parts[1].strip()

        # 找该用户最近一条 pending 记录（source 为空的）
        target_pid = None
        for pid, rec in pending_queue.items():
            if rec.get("uid") == uid and rec.get("gid") == gid and not rec.get("source"):
                target_pid = pid
                break

        if not target_pid:
            yield event.plain_result("没有找到你待审核的、缺少来源的提交哦~")
            return

        pending_queue[target_pid]["source"] = source
        if pending_queue[target_pid].get("status") == ReviewStatus.NEED_SOURCE:
            pending_queue[target_pid]["status"] = ReviewStatus.PENDING
        save_pending()
        char_name = pending_queue[target_pid]["char_name"]
        yield event.plain_result(f"已补充来源：《{source}》{char_name}，等待管理员审核~")

    async def my_wife_submissions(self, event: AstrMessageEvent):
        """查看当前用户最近的添老婆申请状态"""
        uid = str(event.get_sender_id())
        gid = str(event.message_obj.group_id)
        items = [
            (pid, rec) for pid, rec in pending_queue.items()
            if rec.get("uid") == uid and rec.get("gid") == gid
        ]
        if not items:
            yield event.plain_result("你还没有添老婆申请。可以发「添老婆 角色名/作品名」试试~")
            return

        lines = ["你的添老婆申请："]
        for pid, rec in items[-8:]:
            src = f"《{rec.get('source', '')}》" if rec.get("source") else "（来源未知）"
            status = ReviewStatus.label(rec.get("status", ReviewStatus.PENDING))
            lines.append(f"- {src}{rec.get('char_name', '')}：{status}（{pid}）")
        lines.append("来源未知的条目可用「补充来源 作品名」补上。")
        yield event.plain_result("\n".join(lines))

    async def _refresh_list_cache(self):
        """定时拉取 list.txt 缓存到本地，每小时刷新一次，同步更新内存缓存"""
        global _list_cache_mem
        img_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp")
        cache_path = os.path.join(CONFIG_DIR, "list_cache.txt")
        while True:
            try:
                url = self.image_list_url
                if url:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                            if r.status == 200:
                                text = await r.text()
                                with open(cache_path, "w", encoding="utf-8") as f:
                                    f.write(text)
                                # 同步更新内存缓存
                                _list_cache_mem = [
                                    l.strip() for l in text.splitlines()
                                    if l.strip() and "!" in l and l.strip().lower().endswith(img_exts)
                                ]
                                logger.info(f"[list缓存] 已更新，共 {len(_list_cache_mem)} 条有效图片")
            except Exception as e:
                logger.error(f"[添老婆] 拉取 list 缓存失败: {e}")
            await asyncio.sleep(3600)  # 每小时刷新一次

    # ── 英文名缓存 ──────────────────────────────────────────────────────────

    def _load_en_cache(self) -> dict:
        return self.translation_cache.load()

    def _save_en_cache(self, cache: dict) -> None:
        self.translation_cache.save(cache)

    def _get_en_name_from_cache(self, char: str, source: str = "") -> str | None:
        return self.translation_cache.get_en_name(char, source)

    def _cache_key(self, char: str, source: str = "") -> str:
        return self.translation_cache.key(char, source)

    def _normalize_translation_entry(self, entry: dict, char: str = "", source: str = "") -> dict:
        return self.translation_cache.normalize(entry, char, source)

    def _get_translation_from_cache(self, char: str, source: str = "") -> dict | None:
        return self.translation_cache.get_profile(char, source)

    def _write_translation_cache_sync(self, char: str, source: str, result: dict) -> None:
        self.translation_cache.write_profile(char, source, result)

    def _write_en_name_cache_sync(self, char: str, source: str, en_name: str, alt_chars: list | None = None) -> None:
        self.translation_cache.write_en_name(char, source, en_name, alt_chars)

    def _load_existing_chars(self) -> list[tuple[str, str]]:
        """从 list_cache.txt 读取所有已存在的角色，返回 [(source, char_name), ...]"""
        list_txt = os.path.join(CONFIG_DIR, "list_cache.txt")
        chars = []
        if not os.path.exists(list_txt):
            return chars
        try:
            with open(list_txt, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or "!" not in line:
                        continue
                    # 格式: img1/作品名!角色名.jpg 或 作品名!角色名.jpg
                    after_slash = line.split("/", 1)[-1]
                    source, rest = after_slash.split("!", 1)
                    char_name = re.sub(r'(_\d+)?\.[^.]+$', '', rest)
                    if char_name:
                        chars.append((source.strip(), char_name.strip()))
        except Exception:
            pass
        return chars


    async def _lookup_char_en_name(self, char_name: str, source: str = "") -> tuple[str, list[str]]:
        """联网查角色英文名：Bangumi 查日文原名 → AniList 查英文名。
        返回 (en_name, alt_list)，查不到返回 ("", [])。
        """
        def _src_match(a: str, b: str) -> bool:
            a, b = a.strip().lower(), b.strip().lower()
            return bool(a and b and (b in a or a in b))

        # ── Step 1：Bangumi 查日文原名 ──────────────────────────────
        ja_name = ""
        try:
            url = "https://api.bgm.tv/v0/search/characters"
            headers = {"User-Agent": "astrbot_plugin_animewifex/1.0", "Content-Type": "application/json"}
            body = {"keyword": char_name, "filter": {}}
            params = {"limit": 10, "offset": 0}
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=body, params=params, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        for item in (data.get("data") or []):
                            if item.get("gender") == "male":
                                continue
                            item_name = item.get("name", "")
                            if not item_name:
                                continue
                            # source 过滤：优先 infobox，没有则用 summary 宽松匹配
                            if source:
                                item_src = ""
                                for info in item.get("infobox", []):
                                    if info.get("key") in ("登场作品", "组合", "出处", "所属作品", "来源作品"):
                                        val = info.get("value", "")
                                        if isinstance(val, list):
                                            val = val[0].get("v", "") if val else ""
                                        item_src = str(val).strip()
                                        if item_src:
                                            break
                                # infobox 没有则从 summary 里宽松匹配作品名
                                if not item_src:
                                    item_src = item.get("summary", "")
                                if not _src_match(item_src, source):
                                    continue
                            # 验证 name 含日文字符，才拿去搜 AniList
                            if any("\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff" for c in item_name):
                                ja_name = item_name
                            logger.info("[lookup] Bangumi 命中: %r -> name=%r ja=%r" % (char_name, item_name, ja_name))
                            break
        except Exception as e:
            logger.warning("[lookup] Bangumi 查询失败: %s" % e)

        # ── Step 2：AniList 用日文原名查英文名（查不到日文名则放弃）────
        if not ja_name:
            return "", []  # Bangumi 没找到日文名，无法精确查 AniList，不如直接 LLM 兜底
        search_name = ja_name
        try:
            gql = """
query ($search: String) {
  Page(page: 1, perPage: 10) {
    characters(search: $search) {
      name { full native }
      gender
      media(perPage: 1) { nodes { title { native romaji english } } }
    }
  }
}"""
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://graphql.anilist.co",
                    json={"query": gql, "variables": {"search": search_name}},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        chars = data.get("data", {}).get("Page", {}).get("characters", [])
                        for c in chars:
                            if (c.get("gender") or "").lower() == "male":
                                continue
                            full = (c["name"].get("full") or "").strip()
                            native = (c["name"].get("native") or "").strip()
                            if not full:
                                continue
                            # source 过滤
                            if source:
                                media_nodes = (c.get("media") or {}).get("nodes", [])
                                ani_src = ""
                                if media_nodes:
                                    t = media_nodes[0].get("title", {})
                                    ani_src = t.get("native") or t.get("romaji") or t.get("english") or ""
                                if not _src_match(ani_src, source):
                                    continue
                            # 确认 native 和 ja_name 对得上（避免同名不同角色）
                            if ja_name and native and ja_name != native:
                                continue
                            logger.info("[lookup] AniList 命中: %r -> en=%r" % (search_name, full))
                            return full, []
        except Exception as e:
            logger.warning("[lookup] AniList 查询失败: %s" % e)

        return "", []

    async def _write_en_cache_entry(self, char_name: str, source: str = "") -> None:
        """把单个角色的英文名写入缓存。先联网查 Bangumi+AniList，查不到才 LLM 兜底。"""
        cache_key = f"{char_name}|{source}" if source else char_name
        cache = self._load_en_cache()
        if cache_key in cache:
            return  # 已有，跳过
        # 先联网精确查
        en, alt = await self._lookup_char_en_name(char_name, source)
        if not en:
            # 联网查不到，LLM 兜底
            trans = await self._ai_translate_multi(char=char_name, source=source)
            en = (trans.get("en_char") or "").strip()
            alt = [a.strip() for a in (trans.get("alt_char") or []) if a.strip()]
        if en:
            cached_trans = self._get_translation_from_cache(char_name, source) or {}
            cache[cache_key] = {
                **cached_trans,
                "en_char": en,
                "en": en,
                "alt_char": alt,
                "alt": alt,
            }
            self._save_en_cache(cache)
            logger.info("[缓存] 写入: %r -> en=%r alt=%s" % (cache_key, en, alt))

    async def _char_exists_in_list(self, char_name: str, source: str = "") -> str | None:
        """检查角色是否已在 list.txt 中。返回命中的已存在角色名，未命中返回 None。

        查询顺序：
        1. 精确字符串匹配（char_name 相同）
        2. 英文名/别名模糊匹配，但要求 source 也能对上，避免同名不同作误判

        """
        existing_pairs = self._load_existing_chars()
        if not existing_pairs:
            return None

        existing_char_names = {c for _, c in existing_pairs}  # set for O(1) lookup

        # 1. 精确匹配角色名
        if char_name in existing_char_names:
            return char_name

        # 2. 英文名模糊匹配（带 source 校验）
        try:
            trans = await self._ai_translate_multi(char=char_name, source=source)
            en_input  = (trans.get("en_char") or "").strip().lower()
            alt_input = {a.strip().lower() for a in (trans.get("alt_char") or []) if a.strip()}
            check = ({en_input} | alt_input) - {""}
            if not check:
                return None

            # 翻译输入角色的 source，用于比对
            en_source_input = (trans.get("en_source") or "").strip().lower()
            ja_source_input = (trans.get("ja_source") or "").strip().lower()
            short_source_input = (trans.get("short_source") or "").strip().lower()

            cache = self._load_en_cache()
            # cache key 可能是 "char|source" 或旧格式 "char"，两种都要查
            for cache_key, entry in cache.items():
                # 解析 cache key
                if "|" in cache_key:
                    ex_char, ex_src = cache_key.split("|", 1)
                else:
                    ex_char, ex_src = cache_key, ""
                if ex_char == char_name:
                    continue
                if ex_char not in existing_char_names:
                    continue  # 缓存里有但 list 里已删除的，跳过

                en_ex  = (entry.get("en") or "").strip().lower()
                alt_ex = {a.strip().lower() for a in (entry.get("alt") or []) if a.strip()}
                ex_names = ({en_ex} | alt_ex) - {""}
                if not (check & ex_names):
                    continue  # 英文名不撞，跳过

                # 英文名撞上了 → 校验 source
                existing_sources = {s.strip().lower() for s, c in existing_pairs if c == ex_char and s.strip()}
                # 也把 cache key 里的 source 加进来
                if ex_src:
                    existing_sources.add(ex_src.strip().lower())
                if existing_sources:
                    input_sources = {s for s in [en_source_input, ja_source_input, short_source_input, source.strip().lower()] if s}
                    if not (existing_sources & input_sources):
                        logger.info(
                            "[添老婆] 同名不同作，放行: %r (input_src=%s) vs %r (list_src=%s)"
                            % (char_name, input_sources, ex_char, existing_sources)
                        )
                        continue

                logger.info(
                    "[添老婆] 模糊去重命中: %r ≈ %r (en:%s ∩ %s)"
                    % (char_name, ex_char, check, ex_names)
                )
                return ex_char
        except Exception as e:
            logger.warning("[添老婆] 模糊去重失败，放行: %s" % e)

        return None


    async def _bg_flush_drawn_pool(self):
        """后台定时任务：每5分钟把去重池脏数据写盘，减少 IO 频率。"""
        while True:
            await asyncio.sleep(300)  # 5分钟
            global _drawn_pool_dirty
            if _drawn_pool_dirty:
                try:
                    save_drawn_pool()
                    logger.info("[去重池] 定时写盘完成")
                except Exception as e:
                    logger.error(f"[去重池] 定时写盘失败: {e}")

    async def _bg_fill_en_cache(self):
        """启动后台任务：静默补全存量角色的英文名缓存。

        每批 10 条并发翻译，批次间 sleep 3 秒限速，
        避免和正常请求争抢 NV API 配额。
        只处理缓存里缺失的角色，已有条目跳过。
        """
        # 等 list_cache.txt 拉取完成
        await asyncio.sleep(10)

        existing_pairs = self._load_existing_chars()
        cache    = self._load_en_cache()
        # 去重：同一 (char_name, source) 组合只处理一次
        seen: set[tuple[str, str]] = set()
        for source, char_name in existing_pairs:
            seen.add((char_name, source))
        todo = [(char, src) for char, src in seen if (f"{char}|{src}" if src else char) not in cache]

        if not todo:
            logger.info("[缓存] 后台补全：无需更新，共 %d 条已缓存" % len(cache))
            return

        logger.info("[缓存] 后台补全启动，待处理 %d / %d 条" % (len(todo), len(seen)))

        done = 0
        for idx, (char_name, src) in enumerate(todo):
            try:
                cache_key = f"{char_name}|{src}" if src else char_name
                en, alt = await self._lookup_char_en_name(char_name, src)
                if not en:
                    res = await self._ai_translate_multi(char=char_name, source=src)
                    en  = (res.get("en_char") or "").strip()
                    alt = [a.strip() for a in (res.get("alt_char") or []) if a.strip()]
                if en:
                    cached_trans = self._get_translation_from_cache(char_name, src) or {}
                    cache[cache_key] = {
                        **cached_trans,
                        "en_char": en,
                        "en": en,
                        "alt_char": alt,
                        "alt": alt,
                    }
                    done += 1
                    if done % 100 == 0:
                        self._save_en_cache(cache)
                        logger.info("[缓存] 后台补全进度: %d / %d" % (idx + 1, len(todo)))
            except Exception as e:
                logger.warning("[缓存] 后台补全失败 %r: %s" % (char_name, e))

            await asyncio.sleep(1.5)

        self._save_en_cache(cache)
        logger.info("[缓存] 后台补全完成，本次新增 %d 条，缓存共 %d 条" % (done, len(cache)))

    async def rebuild_en_cache(self, event: AstrMessageEvent):
        """管理员命令：刷新缓存 —— 全量重建英文名缓存，分批翻译并回报进度"""
        uid = str(event.get_sender_id())
        if uid not in self.admins:
            yield event.plain_result("只有管理员才能刷新缓存哦~")
            return

        existing_pairs = self._load_existing_chars()
        cache = self._load_en_cache()
        # 去重：同一 (char_name, source) 组合只处理一次
        seen: set[tuple[str, str]] = set()
        for source, char_name in existing_pairs:
            seen.add((char_name, source))
        todo = [(char, src) for char, src in seen if (f"{char}|{src}" if src else char) not in cache]

        if not todo:
            yield event.plain_result(f"缓存已是最新，共 {len(cache)} 条，无需刷新~")
            return

        yield event.plain_result(
            f"开始刷新英文名缓存，共 {len(seen)} 条角色，"
            f"已缓存 {len(cache)} 条，待处理 {len(todo)} 条...\n每条间隔 1.5 秒，请耐心等待~"
        )

        done = 0
        failed = 0
        for idx, (char_name, src) in enumerate(todo):
            try:
                cache_key = f"{char_name}|{src}" if src else char_name
                en, alt = await self._lookup_char_en_name(char_name, src)
                if not en:
                    res = await self._ai_translate_multi(char=char_name, source=src)
                    en  = (res.get("en_char") or "").strip()
                    alt = [a.strip() for a in (res.get("alt_char") or []) if a.strip()]
                if en:
                    cached_trans = self._get_translation_from_cache(char_name, src) or {}
                    cache[cache_key] = {
                        **cached_trans,
                        "en_char": en,
                        "en": en,
                        "alt_char": alt,
                        "alt": alt,
                    }
                    done += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

            await asyncio.sleep(1.5)

            processed = idx + 1
            if processed % 100 == 0 or processed >= len(todo):
                self._save_en_cache(cache)
                yield event.plain_result(
                    f"进度：{processed}/{len(todo)}，成功 {done} 条，失败 {failed} 条..."
                )

        self._save_en_cache(cache)
        yield event.plain_result(
            f"✅ 缓存刷新完成！共 {len(cache)} 条，本次新增 {done} 条，失败 {failed} 条"
        )

    async def _search_female_characters(self, name: str, limit: int = 5, source: str = "") -> list[dict]:
        """Compatibility wrapper around CharacterResolver."""
        return await self.character_resolver.search_female_characters(name, limit=limit, source=source)

    async def _search_bangumi(self, name: str, limit: int) -> list[dict]:
        """Compatibility wrapper around CharacterResolver."""
        return await self.character_resolver.search_bangumi(name, limit)

    async def _search_anilist(self, name: str, limit: int) -> list[dict]:
        """Compatibility wrapper around CharacterResolver."""
        return await self.character_resolver.search_anilist(name, limit)

    async def _search_vndb_characters(self, name: str, limit: int, source: str = "") -> list[dict]:
        """Compatibility wrapper around CharacterResolver."""
        return await self.character_resolver.search_vndb_characters(name, limit, source=source)

    async def _submit_pending(self, gid: str, uid: str, nick: str, chosen: dict, umo: str = ""):
        """存入待审核队列并私聊管理员"""
        import time
        # 队列超500条时清理已完成的旧记录
        if len(pending_queue) > 500:
            done_pids = [
                pid for pid, rec in pending_queue.items()
                if rec.get("status") in ReviewStatus.DONE
            ]
            to_remove = done_pids[:max(0, len(pending_queue) - 400)]
            for pid in to_remove:
                del pending_queue[pid]
            if to_remove:
                save_pending()
                logger.info(f"[pending] 自动清理旧记录 {len(to_remove)} 条")
        pid = f"{gid}_{uid}_{int(time.time())}"
        # 从 umo 提取平台前缀，如 "ATRI:GroupMessage:xxx" -> "ATRI"
        platform = umo.split(":")[0] if umo else "default"
        pending_queue[pid] = {
            "pid": pid,
            "gid": gid,
            "uid": uid,
            "nick": nick,
            "char_name": chosen["name"],
            "source": chosen.get("source", ""),
            "thumb_url": chosen.get("thumb_url", ""),
            "manual_reason": chosen.get("manual_reason", ""),
            "status": ReviewStatus.PENDING if chosen.get("source", "") else ReviewStatus.NEED_SOURCE,
            "submit_time": int(time.time()),
            "platform": platform,
        }
        save_pending()
        await self._notify_admin_pending(pid)

    async def _handle_img_confirm(self, event: AstrMessageEvent, pid: str, action: str):
        """处理管理员图片确认：选N/确认/换图/跳过
        action 取值：
          "选:N"  — 用第 N 张图创建 PR
          "确认"  — 用所有图创建 PR（兼容旧指令）
          "换图"  — 重新拉图
          "跳过"  — 创建空 PR
        """
        import time as _t
        rec = pending_queue.get(pid)
        if not rec:
            yield event.plain_result(f"找不到记录：{pid}")
            return

        src = f"《{rec['source']}》" if rec.get("source") else ""
        img_dir = self._get_img_dir(rec.get("source", ""))

        if action.startswith("选:") or action == "确认":
            session = admin_img_sessions.pop(pid, None)
            if not session or not session.get("images"):
                yield event.plain_result("找不到待确认图片，请重新通过审核")
                return
            all_images = session["images"]

            # 「选 N」只取第 N 张，「确认」取全部
            if action.startswith("选:"):
                try:
                    idx = int(action.split(":", 1)[1]) - 1  # 转 0-based
                    if idx < 0 or idx >= len(all_images):
                        yield event.plain_result(f"编号超出范围，共 {len(all_images)} 张，请选 1~{len(all_images)}")
                        # 把 session 放回去，让管理员重新选
                        admin_img_sessions[pid] = session
                        return
                    images = [all_images[idx]]
                    yield event.plain_result(f"已选第 {idx + 1} 张，正在创建 PR...")
                except ValueError:
                    yield event.plain_result("格式错误，用法：选 N <pid>，N 为图片编号")
                    admin_img_sessions[pid] = session
                    return
            else:
                images = all_images
                yield event.plain_result(f"正在用全部 {len(images)} 张图创建 PR...")

            pr_url = await self._create_github_pr(rec["source"], rec["char_name"], img_dir, images)
            if pr_url:
                rec["status"] = ReviewStatus.PR_CREATED
                rec["pr_url"] = pr_url
                save_pending()
                yield event.plain_result(
                    f"✅ PR 已创建：\n{pr_url}\n"
                    f"merge 完成后发「pr上线 {pid}」通知群友~"
                )
            else:
                yield event.plain_result("PR 创建失败，请检查 github_token 和仓库配置")

        elif action == "换图":
            yield event.plain_result(f"正在重新拉取「{src}{rec['char_name']}」图片...")
            images = await self._fetch_character_images(
                rec["char_name"], rec.get("source", ""), count=3, fallback_thumb_url=rec.get("thumb_url", "")
            )
            if not images:
                yield event.plain_result("仍未找到图片，请用「跳过」走手动流程")
                return
            admin_img_sessions[pid] = {
                "images": images,
                "img_dir": img_dir,
                "expire_time": _t.time() + 300,
            }
            rec["status"] = ReviewStatus.IMAGE_READY
            save_pending()
            platform = rec.get("platform", "default")
            admin_umo = f"{platform}:FriendMessage:{self.admin_qq}"
            await self.context.send_message(
                admin_umo,
                MessageChain().message(
                    f"重新拉到 {len(images)} 张，逐张发送：\n"
                    f"「选 N {pid}」→ 用第 N 张\n"
                    f"「换图 {pid}」→ 再换一批\n"
                    f"「跳过 {pid}」→ 创建空 PR"
                ),
            )
            for i, img_bytes in enumerate(images, 1):
                try:
                    tmp = f"/tmp/_admin_review_{pid}_new_{i}.jpg"
                    with open(tmp, "wb") as f:
                        f.write(img_bytes)
                    await self.context.send_message(
                        admin_umo,
                        MessageChain().message(f"第 {i} 张 / 共 {len(images)} 张："),
                    )
                    await self.context.send_message(
                        admin_umo,
                        MessageChain([Image.fromFileSystem(tmp)]),
                    )
                except Exception as e:
                    logger.warning(f"[审核] 发换图{i}失败: {e}")

        elif action == "跳过":
            admin_img_sessions.pop(pid, None)
            yield event.plain_result(f"正在为「{src}{rec['char_name']}」创建空 PR...")
            pr_url = await self._create_github_pr_empty(rec["source"], rec["char_name"], img_dir)
            if pr_url:
                rec["status"] = ReviewStatus.PR_CREATED
                rec["pr_url"] = pr_url
                save_pending()
                yield event.plain_result(
                    f"PR 已创建：\n{pr_url}\n"
                    f"请上传图片到分支 {img_dir}/ 目录后 merge，\n"
                    f"merge 完成后发「pr上线 {pid}」通知群友~"
                )
            else:
                yield event.plain_result("PR 创建失败，请检查 github_token 和仓库配置")

    async def _notify_admin_pending(self, pid: str):
        """私聊管理员发送审核请求"""
        if not self.admin_qq:
            return
        rec = pending_queue.get(pid)
        if not rec:
            return
        src = f"《{rec['source']}》" if rec['source'] else ""
        text = (
            f"【添老婆审核】\n"
            f"提交人：{rec['nick']}（{rec['uid']}）\n"
            f"角色：{src}{rec['char_name']}\n"
            f"群组：{rec['gid']}\n" +
            (f"备注：{rec.get('manual_reason')}\n" if rec.get("manual_reason") else "") +
            "\n"
            f"回复「通过 {pid}」或「拒绝 {pid}」" +
            ("\n⚠️ 来源未知，可用「通过 " + pid + " 作品名」同时补充来源" if not rec['source'] else "")
        )
        platform = rec.get("platform", "default")
        logger.info("[添老婆] 尝试私聊管理员 session=%s:FriendMessage:%s" % (platform, self.admin_qq))
        try:
            await self.context.send_message(
                f"{platform}:FriendMessage:{self.admin_qq}",
                MessageChain().message(text),
            )
            logger.info("[添老婆] 私聊管理员成功")
        except Exception as e:
            logger.error(f"[添老婆] 私聊管理员失败: {e}")

    async def _handle_private_review(self, event: AstrMessageEvent):
        """处理管理员私聊审核回复"""
        uid = str(event.get_sender_id())
        if uid != str(self.admin_qq):
            return

        # 清理过期的图片确认会话，防止图片bytes堆积内存
        import time as _t
        expired = [pid for pid, s in admin_img_sessions.items() if s.get("expire_time", 0) < _t.time()]
        for pid in expired:
            admin_img_sessions.pop(pid, None)

        msg = event.message_str.strip()

        # ── 选 N：指定用第 N 张图创建 PR ────────────────────────────────────
        # 格式：选 N <pid>  例：选 2 gid_uid_1234567890
        if msg.startswith("选 ") or msg.startswith("选"):
            parts_xuan = msg.split(maxsplit=2)
            # 必须是 "选 <数字> <pid>" 三段
            if len(parts_xuan) == 3 and parts_xuan[1].isdigit():
                target_pid = self._resolve_pid(parts_xuan[2].strip())
                if not target_pid:
                    yield event.plain_result(f"找不到记录：{parts_xuan[2].strip()}")
                    return
                action = f"选:{parts_xuan[1]}"
                async for res in self._handle_img_confirm(event, target_pid, action):
                    yield res
                return

        # ── 图片确认：确认/换图/跳过 ────────────────────────────────────────
        for cmd in ("确认", "换图", "跳过"):
            if msg.startswith(cmd + " ") or msg == cmd:
                parts_cmd = msg.split(maxsplit=1)
                if len(parts_cmd) < 2:
                    yield event.plain_result(f"用法：{cmd} <pid>")
                    return
                target_pid = self._resolve_pid(parts_cmd[1].strip())
                if not target_pid:
                    yield event.plain_result(f"找不到记录：{parts_cmd[1].strip()}")
                    return
                async for res in self._handle_img_confirm(event, target_pid, cmd):
                    yield res
                return

        # ── 拉取老婆审核 ────────────────────────────────────────────────────
        if msg == "拉取老婆审核":
            pendings = {
                pid: rec for pid, rec in pending_queue.items()
                if rec.get("status", ReviewStatus.PENDING) in ReviewStatus.OPEN
            }
            if not pendings:
                yield event.plain_result("✅ 当前没有待审核的添老婆申请。")
                return
            self._review_index = {}
            lines = ["📋 待审核老婆申请（共 " + str(len(pendings)) + " 条）\n"]
            for i, (pid, rec) in enumerate(pendings.items(), 1):
                self._review_index[i] = pid
                src = "《" + rec.get("source", "") + "》" if rec.get("source") else "（来源未知）"
                entry = "[" + str(i) + "] 👤" + rec["nick"] + "（" + rec["uid"] + "）\n"
                entry += "    角色：" + src + rec["char_name"] + "\n"
                entry += "    群：" + rec["gid"]
                if rec.get("manual_reason"):
                    entry += "\n    备注：" + rec["manual_reason"]
                if not rec.get("source"):
                    entry += "\n    ⚠️ 来源未知，可用「通过 " + str(i) + " 作品名」补充"
                lines.append(entry)
            lines.append("\n──────────────────\n指令说明：\n通过 1          → 通过第1条\n通过 1 魔法少女小圆   → 通过并补充来源\n拒绝 1          → 拒绝第1条\npr上线 1        → 通知群友上线")
            yield event.plain_result("\n".join(lines))
            return

        # pr上线 序号/pid
        if msg.startswith("pr上线"):
            parts = msg.split(maxsplit=1)
            if len(parts) < 2:
                yield event.plain_result("用法：pr上线 <序号> 或 pr上线 <pid>")
                return
            pid = self._resolve_pid(parts[1].strip())
            if not pid:
                yield event.plain_result(f"找不到记录：{parts[1].strip()}，请重新「拉取老婆审核」")
                return
            rec = pending_queue.get(pid)
            if not rec:
                yield event.plain_result(f"找不到记录：{pid}")
                return
            if rec.get("status") == ReviewStatus.ONLINE:
                yield event.plain_result("该角色已经上线过了~")
                return
            rec["status"] = ReviewStatus.ONLINE
            save_pending()
            src = f"《{rec['source']}》" if rec.get("source") else ""
            char_name = rec["char_name"]
            platform = rec.get("platform", "default")
            await self._notify_group_at(
                rec["gid"], rec["uid"],
                f"你提交的{src}{char_name}审核通过并已上线啦！快去「抽老婆」试试看~🎉",
                platform,
            )
            yield event.plain_result(f"✅ 已通知群 {rec['gid']}：{src}{char_name} 上线成功~")
            return

        parts = msg.split(maxsplit=2)
        if len(parts) < 2 or parts[0] not in ("通过", "拒绝"):
            return

        action = parts[0]
        pid = self._resolve_pid(parts[1].strip())
        if not pid:
            yield event.plain_result(f"找不到记录：{parts[1].strip()}，请重新「拉取老婆审核」")
            return
        override_source = parts[2].strip() if len(parts) == 3 else None
        rec = pending_queue.get(pid)
        if not rec:
            yield event.plain_result(f"找不到审核记录：{pid}")
            return

        if action == "拒绝":
            rec["status"] = ReviewStatus.REJECTED
            save_pending()
            src = f"《{rec['source']}》" if rec['source'] else ""
            yield event.plain_result(f"已拒绝「{src}{rec['char_name']}」")
            platform = rec.get("platform", "default")
            await self._notify_group_at(rec["gid"], rec["uid"], f"你提交的「{src}{rec['char_name']}」审核未通过~", platform)
            return

        if rec.get("status") in ReviewStatus.LOCKED:
            yield event.plain_result(f"该申请已处理过（状态：{ReviewStatus.label(rec['status'])}），请勿重复操作")
            return
        # ── 通过：先拉图私聊确认，再创建 PR ──────────────────────────────
        if override_source:
            rec["source"] = override_source
        rec["status"] = ReviewStatus.APPROVED
        save_pending()
        src = f"《{rec['source']}》" if rec['source'] else ""
        # 后台写入英文名缓存，不阻塞审核流程
        asyncio.create_task(self._write_en_cache_entry(rec["char_name"], source=rec.get("source", "")))

        img_dir = self._get_img_dir(rec["source"])
        yield event.plain_result(f"已通过「{src}{rec['char_name']}」，正在拉取图片供确认...")

        images = await self._fetch_character_images(
            rec["char_name"], rec.get("source", ""), count=3, fallback_thumb_url=rec.get("thumb_url", "")
        )

        if not images:
            # 拉不到图，直接走空 PR 流程
            yield event.plain_result("未找到图片，直接创建空 PR...")
            pr_url = await self._create_github_pr_empty(rec["source"], rec["char_name"], img_dir)
            if pr_url:
                rec["status"] = ReviewStatus.PR_CREATED
                rec["pr_url"] = pr_url
                save_pending()
                yield event.plain_result(
                    f"PR 已创建：\n{pr_url}\n"
                    f"请上传图片到分支 {img_dir}/ 目录后 merge，\n"
                    f"merge 完成后发「pr上线 {pid}」通知群友~"
                )
            else:
                yield event.plain_result("PR 创建失败，请检查 github_token 和仓库配置")
            return

        # 存入 admin_img_sessions，等待确认
        import time as _t
        admin_img_sessions[pid] = {
            "images": images,
            "img_dir": img_dir,
            "expire_time": _t.time() + 300,
        }
        rec["status"] = ReviewStatus.IMAGE_READY
        save_pending()

        # 私聊发图：逐张带编号，管理员可回「选 N」指定用哪张
        platform = rec.get("platform", "default")
        admin_umo = f"{platform}:FriendMessage:{self.admin_qq}"
        try:
            await self.context.send_message(
                admin_umo,
                MessageChain().message(
                    f"「{src}{rec['char_name']}」找到 {len(images)} 张候选图，逐张发送，看好后回复：\n"
                    f"「选 N {pid}」→ 用第 N 张创建 PR（如：选 2 {pid}）\n"
                    f"「换图 {pid}」→ 重新拉一批\n"
                    f"「跳过 {pid}」→ 创建空 PR（手动传图）"
                ),
            )
            for i, img_bytes in enumerate(images, 1):
                try:
                    tmp = f"/tmp/_admin_review_{pid}_{i}.jpg"
                    with open(tmp, "wb") as f:
                        f.write(img_bytes)
                    # 先发编号文字，再发图
                    await self.context.send_message(
                        admin_umo,
                        MessageChain().message(f"第 {i} 张 / 共 {len(images)} 张："),
                    )
                    await self.context.send_message(
                        admin_umo,
                        MessageChain([Image.fromFileSystem(tmp)]),
                    )
                except Exception as e:
                    logger.warning(f"[审核] 发图{i}失败: {e}")
        except Exception as e:
            logger.error(f"[审核] 私聊发图失败: {e}")
            yield event.plain_result(f"私聊发图失败（{e}），回复「选 N {pid}」/「换图 {pid}」/「跳过 {pid}」继续")

    async def pr_online(self, event: AstrMessageEvent):
        """管理员命令：pr上线 pid —— merge后艾特提交者通知审核通过上线"""
        uid = str(event.get_sender_id())
        if uid != str(self.admin_qq):
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：pr上线 <pid>")
            return

        pid = parts[1].strip()
        rec = pending_queue.get(pid)
        if not rec:
            yield event.plain_result(f"找不到记录：{pid}")
            return

        if rec.get("status") == ReviewStatus.ONLINE:
            yield event.plain_result("该角色已经上线过了~")
            return

        rec["status"] = ReviewStatus.ONLINE
        save_pending()

        src = f"《{rec['source']}》" if rec.get("source") else ""
        char_name = rec["char_name"]
        submitter_uid = rec["uid"]
        gid = rec["gid"]
        platform = rec.get("platform", "default")

        await self._notify_group_at(
            gid, submitter_uid,
            f"你提交的{src}{char_name}审核通过并已上线啦！快去「抽老婆」试试看~🎉",
            platform,
        )
        yield event.plain_result(f"✅ 已通知群 {gid}：{src}{char_name} 上线成功~")

    async def _fetch_character_images(
        self, char_name: str, source: str, count: int = 3, fallback_thumb_url: str = ""
    ) -> list[bytes]:
        """Fetch candidate review images through the image service."""
        return await self.image_fetcher.fetch_character_images(
            char_name=char_name,
            source=source,
            count=count,
            fallback_thumb_url=fallback_thumb_url,
        )

    def _get_img_dir(self, source: str) -> str:
        return self.github_publisher.get_img_dir(source)

    @staticmethod
    def _detect_img_ext(data: bytes) -> str:
        return GitHubPublisher.detect_img_ext(data)

    async def _create_github_pr_empty(self, source: str, char_name: str, img_dir: str) -> str | None:
        """Create a manual-upload PR through the GitHub publisher service."""
        return await self.github_publisher.create_empty_pr(source, char_name, img_dir)

    async def _create_github_pr(
        self, source: str, char_name: str, img_dir: str, images: list[bytes]
    ) -> str | None:
        """Create an image PR through the GitHub publisher service."""
        return await self.github_publisher.create_pr(source, char_name, img_dir, images)

    async def _notify_group(self, gid: str, text: str):
        """向群发送纯文本通知"""
        try:
            await self.context.send_message(
                f"default:GroupMessage:{gid}",
                MessageChain().message(text),
            )
        except Exception as e:
            logger.error(f"[添老婆] 通知群失败: {e}")

    async def _notify_group_at(self, gid: str, uid: str, text: str, platform: str = "default"):
        """向群发送艾特通知"""
        try:
            await self.context.send_message(
                f"{platform}:GroupMessage:{gid}",
                MessageChain([At(qq=uid), Plain(f" {text}")]),
            )
        except Exception as e:
            logger.error(f"[添老婆] 艾特通知群失败: {e}")

    def _resolve_pid(self, raw: str) -> str | None:
        """将序号或原始pid转换为pid"""
        if raw.isdigit():
            idx = int(raw)
            return getattr(self, "_review_index", {}).get(idx)
        if raw in pending_queue:
            return raw
        return None

    async def terminate(self):
        """插件卸载时清理资源"""
        if _drawn_pool_dirty:
            save_drawn_pool()
        config_locks.clear()
        records.clear()
        swap_requests.clear()
        ntr_statuses.clear()
        add_sessions.clear()
        pending_queue.clear()
        drawn_pool.clear()
        _karma_cache.clear()
