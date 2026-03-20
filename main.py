from astrbot.api.all import *
from astrbot.api.star import StarTools
from datetime import datetime, timedelta
import random
import os
import json
import aiohttp
import asyncio
import io
from PIL import Image as PilImage
import re
from urllib.parse import quote
from .hentai_search import HentaiSearcher

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
EN_CACHE_FILE = os.path.join(CONFIG_DIR, "en_cache.json")  # 角色名→英文名缓存

# ==================== 全局数据存储 ====================

records = {  # 统一的记录数据结构
    "ntr": {},        # 牛老婆记录
    "change": {},     # 换老婆记录
    "reset": {},      # 重置使用次数
    "swap": {},       # 交换老婆请求次数
    "dengge_resets": {}  # 每日重置换成功次数（用于邓哥曾姐概率惩罚）
}
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


# ==================== 数据加载和保存函数 ====================

def load_records():
    """加载所有记录数据"""
    raw = load_json(RECORDS_FILE)
    records.clear()
    records.update({
        "ntr": raw.get("ntr", {}),
        "change": raw.get("change", {}),
        "reset": raw.get("reset", {}),
        "swap": raw.get("swap", {}),
        "dengge_resets": raw.get("dengge_resets", {})
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
        self._hentai_searcher = HentaiSearcher(self.config, en_cache_fn=self._get_en_name_from_cache)
        # 启动时异步拉取 list 缓存
        asyncio.create_task(self._refresh_list_cache())
        # 启动时后台静默补全英文名缓存（限速慢跑，不影响正常使用）
        asyncio.create_task(self._bg_fill_en_cache())

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
        # 邓哥曾姐惩罚相关配置
        self.dengge_img          = self.config.get("dengge_img", "")           # 邓哥图片路径（list.txt 中的相对路径）
        self.zengjie_img         = self.config.get("zengjie_img", "")          # 曾姐图片路径（list.txt 中的相对路径）
        self.dengge_base_prob    = self.config.get("dengge_base_prob", 0.15)   # 每次重置换成功后增加的概率
        self.dengge_max_prob     = self.config.get("dengge_max_prob", 0.80)    # 概率上限

    def _init_commands(self):
        """初始化命令映射表"""
        self.commands = {
            "老婆帮助": self.wife_help,
            "抽老婆": self.animewife,
            "查老婆": self.search_wife,
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
            "补充来源": self.add_wife_source,
            "刷新缓存": self.rebuild_en_cache,
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

        for cmd, func in self.commands.items():
            if text.startswith(cmd):
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

        # ── 邓哥曾姐惩罚判定 ──
        # 条件：今日有人给自己重置换成功过 且 至少配置了一张惩罚图
        punishment_imgs = [img for img in [self.dengge_img, self.zengjie_img] if img]
        if punishment_imgs:
            dengge_rec = records["dengge_resets"].get(gid, {}).get(uid, {})
            reset_count = dengge_rec.get("count", 0) if dengge_rec.get("date") == today else 0
            if reset_count > 0:
                dengge_prob = min(reset_count * self.dengge_base_prob, self.dengge_max_prob)
                if random.random() < dengge_prob:
                    prob_pct = int(dengge_prob * 100)
                    chosen_img = random.choice(punishment_imgs)
                    is_dengge = (chosen_img == self.dengge_img)
                    who = "邓哥" if is_dengge else "曾姐"
                    msg = (
                        f"{nick}，你今天换老婆次数已经用完，"
                        f"还重置了 {reset_count} 次（触发概率 {prob_pct}%）——\n"
                        f"天道好轮回，你抽到的老婆是…… {who}！💍\n"
                        f"专情才是真理，明天从头开始吧~"
                    )
                    img_comp = await self._resolve_wife_image(chosen_img)
                    if img_comp:
                        yield event.chain_result([Plain(msg), img_comp])
                    else:
                        yield event.plain_result(msg)
                    return

        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            wife_data = cfg.get(uid)
            
            if not wife_data or not isinstance(wife_data, list) or wife_data[1] != today or not self._is_valid_img_path(wife_data[0]):
                # 今天还没抽，或缓存的是无效路径，重新获取
                img = await self._fetch_wife_image()
                if not img:
                    yield event.plain_result("抱歉，今天的老婆获取失败了，请稍后再试~")
                    return
                cfg[uid] = [img, today, nick]
                save_group_config(gid, cfg)
            else:
                img = wife_data[0]
        
        # 生成并发送消息
        yield event.chain_result(await self._build_wife_message(img, nick))

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
                    return random.choice(lines)
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
                                return random.choice(lines)
        except Exception:
            pass

        return None

    async def _build_wife_message(self, img: str, nick: str):
        """构建老婆消息链"""
        name = os.path.splitext(img)[0].split("/")[-1]
        
        if "!" in name:
            source, chara = name.split("!", 1)
            text = f"{nick}，你今天的老婆是来自《{source}》的{chara}，请好好珍惜哦~\n发送「要本子」看看有没有她的本子~"
        else:
            text = f"{nick}，你今天的老婆是{name}，请好好珍惜哦~\n发送「要本子」看看有没有她的本子~"
        
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

【管理员命令】
• 切换ntr开关状态 - 开启/关闭NTR功能

💡 提示：部分命令有每日使用次数限制
"""
        yield event.plain_result(help_text.strip())

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
        
        img, _, owner = wife_data
        
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
            
            # 更新牛的次数
            rec["count"] += 1
            grp[uid] = rec
            save_records()
            
            # 判断牛老婆是否成功
            if random.random() < self.ntr_possibility:
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
        
        if random.random() < self.reset_success_rate:
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
        
        if random.random() < self.reset_success_rate:
            grp2 = records["change"].setdefault(gid, {})
            if tid in grp2:
                # 重置换成功，累计被重置人的邓哥曾姐惩罚次数
                if self.dengge_img or self.zengjie_img:
                    dg_grp = records["dengge_resets"].setdefault(gid, {})
                    dg_rec = dg_grp.get(tid, {"date": today, "count": 0})
                    if dg_rec.get("date") != today:
                        dg_rec = {"date": today, "count": 0}
                    dg_rec["count"] += 1
                    dg_grp[tid] = dg_rec
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
        
        grp = swap_requests.get(gid, {})
        rec = grp.get(uid)
        
        if not rec or rec.get("target") != tid:
            yield event.plain_result(f"{nick}，请在命令后@发起者，或用\"查看交换请求\"命令查看当前请求哦~")
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

            if not msg.isdigit():
                return  # 不是数字也不是指令，忽略

            idx = int(msg) - 1
            candidates = session.get("candidates", [])
            if not (0 <= idx < len(candidates)):
                yield event.plain_result(f"请输入 1~{len(candidates)} 的数字哦~")
                return

            chosen = candidates[idx]
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
                "expire_time": _time.time() + 60,
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
        all_candidates = await self._search_female_characters(query, limit=offset + 9, source=_hint_source)

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
                yield event.plain_result(
                    f"没找到「{query}」相关角色，换个关键词试试？\n"
                    f"提示：可以用日文名或英文名搜索效果更好"
                )
            else:
                yield event.plain_result("没有更多候选了，换个关键词试试吧~")
            return

        # 发文字列表
        lines = ["找到以下角色，回复数字选择（60秒内有效）："]
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
            "expire_time": time.time() + 60,
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
        save_pending()
        char_name = pending_queue[target_pid]["char_name"]
        yield event.plain_result(f"已补充来源：《{source}》{char_name}，等待管理员审核~")

    async def _refresh_list_cache(self):
        """定时拉取 list.txt 缓存到本地，每小时刷新一次"""
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
                                logger.info(f"[添老婆] list.txt 缓存已更新，共 {len(text.splitlines())} 条")
            except Exception as e:
                logger.error(f"[添老婆] 拉取 list 缓存失败: {e}")
            await asyncio.sleep(3600)  # 每小时刷新一次

    # ── 英文名缓存 ──────────────────────────────────────────────────────────

    def _load_en_cache(self) -> dict:
        """加载英文名缓存 {char_name: {"en": str, "alt": [str]}}"""
        return load_json(EN_CACHE_FILE)

    def _save_en_cache(self, cache: dict) -> None:
        save_json(EN_CACHE_FILE, cache)

    def _get_en_name_from_cache(self, char: str, source: str = "") -> str | None:
        """供 HentaiSearcher 查询英文名的回调，查不到返回 None。"""
        cache = self._load_en_cache()
        # 优先用 char|source 组合键，没有再退回纯 char 键
        for key in (f"{char}|{source}" if source else None, char):
            if not key:
                continue
            entry = cache.get(key)
            if not entry:
                continue
            if isinstance(entry, dict):
                return entry.get("en") or None
            if isinstance(entry, str):
                return entry or None
        return None

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
            cache[cache_key] = {"en": en, "alt": alt}
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
                    cache[cache_key] = {"en": en, "alt": alt}
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
                    cache[cache_key] = {"en": en, "alt": alt}
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
        """搜索女性角色：先 Bangumi 搜中文名，取日文原名再搜 AniList，source 过滤候选"""

        def _src_match(candidate_src: str, filter_src: str) -> bool:
            """宽松的作品名匹配：只要 filter_src 出现在 candidate_src 里（或反向）即算命中"""
            a = candidate_src.strip().lower()
            b = filter_src.strip().lower()
            return bool(a and b and (b in a or a in b))

        seen: set = set()
        results: list = []

        def _add(r: dict):
            key = r["name"].strip().lower()
            if key not in seen:
                seen.add(key)
                results.append(r)

        # ── Step 1：Bangumi 搜原始名（中文/日文都支持）─────────────────
        bgm_results = await self._search_bangumi(name, limit * 3)
        ja_names_from_bgm: list[str] = []
        for r in bgm_results:
            if source and not _src_match(r.get("source", ""), source):
                continue  # source 对不上，跳过
            _add(r)
            # 收集角色名供 AniList 二次搜索（Bangumi name 字段通常是日文原名，AniList 也接受）
            ja = r["name"]
            if ja and ja != name and ja not in ja_names_from_bgm:
                ja_names_from_bgm.append(ja)

        # ── Step 2：用 Bangumi 拿到的日文原名搜 AniList ──────────────
        for ja in ja_names_from_bgm[:2]:  # 最多取前2个日文名去搜
            if len(results) >= limit:
                break
            ani_results = await self._search_anilist(ja, limit)
            for r in ani_results:
                if source and not _src_match(r.get("source", ""), source):
                    continue
                _add(r)

        # ── Step 3：如果结果还不够，直接用原始名搜 AniList 补充 ───────
        if len(results) < limit:
            ani_results = await self._search_anilist(name, limit)
            for r in ani_results:
                if source and not _src_match(r.get("source", ""), source):
                    continue
                _add(r)

        # ── Step 4：如果 source 过滤后结果为空，放开过滤再搜一次 ──────
        if not results:
            for r in bgm_results:
                _add(r)
            if len(results) < limit:
                ani_results = await self._search_anilist(name, limit)
                for r in ani_results:
                    _add(r)

        return results[:limit]


    async def _search_bangumi(self, name: str, limit: int) -> list[dict]:
        """Bangumi v0 POST 搜索角色"""
        try:
            url = "https://api.bgm.tv/v0/search/characters"
            headers = {"User-Agent": "astrbot_plugin_animewifex/1.0", "Content-Type": "application/json"}
            body = {"keyword": name, "filter": {}}
            params = {"limit": limit * 3, "offset": 0}
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=body, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        logger.warning("[添老婆][Bangumi] HTTP %d" % r.status)
                        return []
                    data = await r.json()
        except Exception as e:
            logger.error(f"[添老婆] Bangumi 搜索失败: {e}")
            return []

        items = data.get("data") or []
        logger.info("[添老婆][Bangumi] 搜索 %r 返回%d条: %s" % (name, len(items), [(x.get("name"), x.get("gender")) for x in items[:10]]))
        results = []
        for item in items:
            if item.get("gender") == "male":  # 排除明确男性
                continue
            char_name = item.get("name", "")
            if not char_name:
                continue
            char_source = ""
            for info in item.get("infobox", []):
                key = info.get("key", "")
                if key in ("登场作品", "组合", "出处", "所属作品", "来源作品"):
                    val = info.get("value", "")
                    if isinstance(val, list):
                        val = val[0].get("v", "") if val else ""
                    char_source = str(val).strip()
                    if char_source:
                        break
            images = item.get("images", {})
            thumb = images.get("small") or images.get("medium") or ""
            if thumb and not thumb.startswith("http"):
                thumb = "https:" + thumb
            results.append({"name": char_name, "source": char_source, "thumb_url": thumb})
            if len(results) >= limit:
                break
        logger.info("[添老婆][Bangumi] 过滤后: %s" % [r["name"] for r in results])
        return results

    async def _search_anilist(self, name: str, limit: int) -> list[dict]:
        """AniList GraphQL 搜索角色"""
        query = """
query ($search: String) {
  Page(page: 1, perPage: 20) {
    characters(search: $search) {
      name { full native }
      gender
      image { medium }
      media(perPage: 1) {
        nodes { title { native romaji english } }
      }
    }
  }
}
"""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://graphql.anilist.co",
                    json={"query": query, "variables": {"search": name}},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status != 200:
                        body = await r.text()
                        logger.warning("[添老婆][AniList] HTTP %d body=%s" % (r.status, body[:300]))
                        return []
                    data = await r.json()
        except Exception as e:
            logger.error(f"[添老婆] AniList 搜索失败: {e}")
            return []

        chars = data.get("data", {}).get("Page", {}).get("characters", [])
        logger.info("[添老婆][AniList] 搜索 %r 返回%d条: %s" % (name, len(chars), [(c.get("name",{}).get("full"), c.get("gender")) for c in chars[:10]]))
        def _is_japanese(s):
            return any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in s)
        def _is_korean(s):
            return any("\uac00" <= ch <= "\ud7a3" for ch in s)
        results = []
        for c in chars:
            gender = (c.get("gender") or "").lower()
            if gender == "male":
                continue
            native = c["name"].get("native") or ""
            full   = c["name"].get("full") or ""
            if native and _is_japanese(native):
                char_name = native
            elif full and not _is_korean(full):
                char_name = full
            elif native:
                char_name = native
            else:
                char_name = full
            if not char_name:
                continue
            media_nodes = (c.get("media") or {}).get("nodes", [])
            source = ""
            if media_nodes:
                t = media_nodes[0].get("title", {})
                source = t.get("native") or t.get("romaji") or t.get("english") or ""
            thumb = (c.get("image") or {}).get("medium") or ""
            results.append({"name": char_name, "source": source, "thumb_url": thumb})
            if len(results) >= limit:
                break
        logger.info("[添老婆][AniList] 过滤后: %s" % [r["name"] for r in results])
        return results

    async def _submit_pending(self, gid: str, uid: str, nick: str, chosen: dict, umo: str = ""):
        """存入待审核队列并私聊管理员"""
        import time
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
            "status": "pending",
            "submit_time": int(time.time()),
            "platform": platform,
        }
        save_pending()
        await self._notify_admin_pending(pid)

    async def _handle_img_confirm(self, event: AstrMessageEvent, pid: str, action: str):
        """处理管理员图片确认：确认/换图/跳过"""
        import time as _t
        rec = pending_queue.get(pid)
        if not rec:
            yield event.plain_result(f"找不到记录：{pid}")
            return

        src = f"《{rec['source']}》" if rec.get("source") else ""
        img_dir = self._get_img_dir(rec.get("source", ""))

        if action == "确认":
            session = admin_img_sessions.pop(pid, None)
            if not session or not session.get("images"):
                yield event.plain_result("找不到待确认图片，请重新通过审核")
                return
            images = session["images"]
            yield event.plain_result(f"正在用{len(images)}张图创建 PR...")
            pr_url = await self._create_github_pr(rec["source"], rec["char_name"], img_dir, images)
            if pr_url:
                rec["status"] = "pr_created"
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
            images = await self._fetch_character_images(rec["char_name"], rec.get("source", ""), count=3)
            if not images:
                yield event.plain_result("仍未找到图片，请用「跳过」走手动流程")
                return
            admin_img_sessions[pid] = {
                "images": images,
                "img_dir": img_dir,
                "expire_time": _t.time() + 300,
            }
            platform = rec.get("platform", "default")
            admin_umo = f"{platform}:FriendMessage:{self.admin_qq}"
            yield event.plain_result(f"新图片（共{len(images)}张）：")
            for img_bytes in images:
                try:
                    tmp = f"/tmp/_shuushuu_admin_{pid}_new_{images.index(img_bytes)}.jpg"
                    with open(tmp, "wb") as f:
                        f.write(img_bytes)
                    await self.context.send_message(
                        admin_umo,
                        MessageChain([Image.fromFileSystem(tmp)]),
                    )
                except Exception as e:
                    logger.warning(f"[审核] 发换图失败: {e}")
            yield event.plain_result(
                f"回复「确认 {pid}」→ 用这批图创建 PR\n"
                f"回复「换图 {pid}」→ 再换一批\n"
                f"回复「跳过 {pid}」→ 创建空 PR"
            )

        elif action == "跳过":
            admin_img_sessions.pop(pid, None)
            yield event.plain_result(f"正在为「{src}{rec['char_name']}」创建空 PR...")
            pr_url = await self._create_github_pr_empty(rec["source"], rec["char_name"], img_dir)
            if pr_url:
                rec["status"] = "pr_created"
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
            f"群组：{rec['gid']}\n\n"
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

        msg = event.message_str.strip()

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
            pendings = {pid: rec for pid, rec in pending_queue.items() if rec.get("status") == "pending"}
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
            if rec.get("status") == "online":
                yield event.plain_result("该角色已经上线过了~")
                return
            rec["status"] = "online"
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
            rec["status"] = "rejected"
            save_pending()
            src = f"《{rec['source']}》" if rec['source'] else ""
            yield event.plain_result(f"已拒绝「{src}{rec['char_name']}」")
            platform = rec.get("platform", "default")
            await self._notify_group_at(rec["gid"], rec["uid"], f"你提交的「{src}{rec['char_name']}」审核未通过~", platform)
            return

        if rec.get("status") in ("approved", "pr_created", "online"):
            yield event.plain_result(f"该申请已处理过（状态：{rec['status']}），请勿重复操作")
            return
        # ── 通过：先拉图私聊确认，再创建 PR ──────────────────────────────
        if override_source:
            rec["source"] = override_source
        rec["status"] = "approved"
        save_pending()
        src = f"《{rec['source']}》" if rec['source'] else ""
        # 后台写入英文名缓存，不阻塞审核流程
        asyncio.create_task(self._write_en_cache_entry(rec["char_name"], source=rec.get("source", "")))

        img_dir = self._get_img_dir(rec["source"])
        yield event.plain_result(f"已通过「{src}{rec['char_name']}」，正在拉取图片供确认...")

        images = await self._fetch_character_images(rec["char_name"], rec.get("source", ""), count=3)

        if not images:
            # 拉不到图，直接走空 PR 流程
            yield event.plain_result("未找到图片，直接创建空 PR...")
            pr_url = await self._create_github_pr_empty(rec["source"], rec["char_name"], img_dir)
            if pr_url:
                rec["status"] = "pr_created"
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

        # 私聊发图
        platform = rec.get("platform", "default")
        admin_umo = f"{platform}:FriendMessage:{self.admin_qq}"
        try:
            await self.context.send_message(
                admin_umo,
                MessageChain().message(
                    f"「{src}{rec['char_name']}」候选图片（共{len(images)}张），请确认：\n"
                    f"回复「确认 {pid}」→ 用这批图创建 PR\n"
                    f"回复「换图 {pid}」→ 重新拉一批\n"
                    f"回复「跳过 {pid}」→ 创建空 PR（手动传图）"
                ),
            )
            for i, img_bytes in enumerate(images, 1):
                try:
                    tmp = f"/tmp/_admin_review_{pid}_{i}.jpg"
                    with open(tmp, "wb") as f:
                        f.write(img_bytes)
                    await self.context.send_message(
                        admin_umo,
                        MessageChain([Image.fromFileSystem(tmp)]),
                    )
                except Exception as e:
                    logger.warning(f"[审核] 发图{i}失败: {e}")
        except Exception as e:
            logger.error(f"[审核] 私聊发图失败: {e}")
            yield event.plain_result(f"私聊发图失败（{e}），回复「确认 {pid}」/「换图 {pid}」/「跳过 {pid}」继续")

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

        if rec.get("status") == "online":
            yield event.plain_result("该角色已经上线过了~")
            return

        rec["status"] = "online"
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

    async def _fetch_character_images(self, char_name: str, source: str, count: int = 3) -> list[bytes]:
        """拉图优先级：Pixiv → Gelbooru → Yande.re → Danbooru → Konachan → DLsite封面（终极保底）"""
        translations = await self._ai_translate_multi(char=char_name, source=source)
        en_name      = translations.get("en_char",     char_name)
        ja_name      = translations.get("ja_char",     char_name)
        kana_name    = translations.get("kana_char",   "")
        alt_chars    = translations.get("alt_char",    [])   # 别名列表
        en_source    = translations.get("en_source",   source)
        ja_source    = translations.get("ja_source",   source)
        short_source = translations.get("short_source", "")  # 同人圈缩写

        def to_tag(s: str) -> str:
            return s.strip().lower().replace(" ", "_").replace("·", "_").replace("・", "_")

        # 角色 booru tag 候选：英文名 → 日文名 → 别名 → 原始输入，全部转下划线格式
        char_tag_sources = [en_name, ja_name] + alt_chars + [char_name]
        booru_char_tags = list(dict.fromkeys(to_tag(s) for s in char_tag_sources if s and s.strip()))

        # 带作品限定的组合 tag（booru 标准写法：角色tag + 作品tag）
        source_tag_sources = [short_source, en_source, ja_source, source]
        booru_src_tags = list(dict.fromkeys(to_tag(s) for s in source_tag_sources if s and s.strip()))

        # booru 搜索顺序：优先「角色+作品」组合，再退回纯角色名
        booru_tags = []
        for ctag in booru_char_tags:
            for stag in booru_src_tags:
                booru_tags.append(f"{ctag} {stag}")  # 组合 tag，命中更精准
        booru_tags += booru_char_tags   # 纯角色名兜底

        # Pixiv 搜索词：优先「角色名 作品名」组合，精准度更高
        pixiv_combined = []
        for cname in [ja_name, en_name, char_name]:
            for sname in [ja_source, short_source, source]:
                if cname and sname and cname.strip() and sname.strip():
                    pixiv_combined.append(f"{cname.strip()} {sname.strip()}")
        pixiv_tag_sources = pixiv_combined + [en_name, ja_name] + alt_chars + [char_name]
        pixiv_tags = list(dict.fromkeys(s.strip() for s in pixiv_tag_sources if s and s.strip()))

        # Getchu/DLsite 作品查询词顺序：日文原名 → 缩写 → 英文 → 原始输入
        source_queries = list(dict.fromkeys(filter(None, [ja_source, short_source, en_source, source])))

        logger.info(
            "[添老婆] 拉图 booru_char_tags=%s booru_src_tags=%s pixiv_tags=%s short_source=%r"
            % (booru_char_tags, booru_src_tags, pixiv_tags, short_source)
        )

        images = []

        # 1. Pixiv 优先
        if self.pixiv_refresh_token and len(images) < count:
            for q in pixiv_tags:
                if len(images) >= count:
                    break
                images.extend(await self._pixiv_fetch(q, count - len(images)))
            logger.info("[添老婆] Pixiv 后共%d张" % len(images))

        # 2. e-shuushuu（有 token 时）
        if len(images) < count and self.config.get("shuushuu_access_token"):
            images.extend(await self._shuushuu_fetch(
                char_name, en_name, kana_name, source, count - len(images)
            ))
            logger.info("[添老婆] shuushuu 后共%d张" % len(images))

        # 3. Gelbooru（rating:general 过滤 R18）
        if len(images) < count:
            for q in booru_tags:
                if len(images) >= count:
                    break
                images.extend(await self._gelbooru_fetch(q, count - len(images)))
            logger.info("[添老婆] Gelbooru 后共%d张" % len(images))

        # 4. Yande.re（rating:safe）
        if len(images) < count:
            for q in booru_tags:
                if len(images) >= count:
                    break
                images.extend(await self._yandere_fetch(q, count - len(images)))
            logger.info("[添老婆] Yande.re 后共%d张" % len(images))

        # 5. Danbooru
        if len(images) < count:
            for q in booru_tags:
                if len(images) >= count:
                    break
                images.extend(await self._danbooru_fetch(q, count - len(images)))
            logger.info("[添老婆] Danbooru 后共%d张" % len(images))

        # 6. Konachan
        if len(images) < count:
            for q in booru_tags:
                if len(images) >= count:
                    break
                images.extend(await self._konachan_fetch(q, count - len(images)))
            logger.info("[添老婆] Konachan 后共%d张" % len(images))

        # 7. Getchu 立绘（官方宣传素材，完全拉不到同人图才走）
        if not images:
            logger.info("[添老婆] 同人图源均失败，尝试 Getchu 官方立绘")
            for q in source_queries:
                imgs = await self._getchu_fetch(q, count)
                if imgs:
                    images.extend(imgs)
                    logger.info("[添老婆] Getchu 后共%d张" % len(images))
                    break

        # 7. DLsite 封面（终极最后保底）
        if not images:
            logger.info("[添老婆] Getchu 也失败，尝试 DLsite 封面保底")
            for q in source_queries:
                imgs = await self._dlsite_cover_fetch(q, count)
                if imgs:
                    images.extend(imgs)
                    logger.info("[添老婆] DLsite封面 后共%d张" % len(images))
                    break

        return images[:count]

    async def _shuushuu_fetch(
        self, char_name: str, en_name: str, kana_name: str, source: str, count: int
    ) -> list[bytes]:
        """e-shuushuu 拉图：VNDB 查英文名 → 查 tag_id → 用作品 tag 搜图过滤角色 tag"""
        images = []
        shuushuu_access  = self.config.get("shuushuu_access_token", "")
        shuushuu_refresh = self.config.get("shuushuu_refresh_token", "")
        if not shuushuu_access:
            return []

        cookie_str = f"access_token={shuushuu_access}; refresh_token={shuushuu_refresh}"
        headers_base = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Cookie": cookie_str,
            "Referer": "https://e-shuushuu.net/",
        }

        try:
            async with aiohttp.ClientSession() as s:
                # Step 1: VNDB 查英文名
                vndb_en = ""
                for sname in list(dict.fromkeys(filter(None, [kana_name, char_name]))):
                    vndb_en = await self._vndb_lookup_en(s, sname, source)
                    if vndb_en:
                        break

                char_query = vndb_en or en_name
                if not char_query:
                    logger.info("[shuushuu] 无英文名，跳过")
                    return []

                logger.info("[shuushuu] char_query=%r source=%r" % (char_query, source))

                # Step 2: 查角色 tag_id
                char_tag_id = await self._shuushuu_find_tag(s, headers_base, char_query, source, tag_type=4)
                if not char_tag_id:
                    logger.info("[shuushuu] 找不到角色 tag，跳过")
                    return []

                # Step 3: 查作品 tag_id（可选）
                source_tag_id = None
                if source:
                    source_tag_id = await self._shuushuu_find_tag(s, headers_base, source, "", tag_type=None)

                # Step 4: 搜图
                images = await self._shuushuu_fetch_images(s, headers_base, char_tag_id, source_tag_id, count)

        except Exception as e:
            logger.error("[shuushuu] 拉图失败: %s" % e)
        return images

    async def _vndb_lookup_en(
        self, session: aiohttp.ClientSession, char_name: str, source: str
    ) -> str:
        """VNDB 查角色英文名，结合 source 过滤"""
        try:
            payload = {
                "filters": ["search", "=", char_name],
                "fields": "id,name,original,vns.title,vns.alttitle",
                "results": 20,
            }
            async with session.post(
                "https://api.vndb.org/kana/character",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return ""
                data = await r.json()
            results = data.get("results", [])
            source_low = source.strip().lower()
            scored = []
            for c in results:
                name = c.get("name", "")
                original = c.get("original") or ""
                vn_titles = " ".join(
                    (v.get("title") or "") + " " + (v.get("alttitle") or "")
                    for v in c.get("vns", [])
                ).lower()
                score = 0
                if original == char_name: score += 10
                if source_low and source_low in vn_titles: score += 15
                scored.append((score, name))
            scored.sort(reverse=True)
            if scored and scored[0][0] > 0:
                logger.info("[shuushuu][VNDB] %r -> %r" % (char_name, scored[0][1]))
                return scored[0][1]
        except Exception as e:
            logger.warning("[shuushuu][VNDB] 查询失败: %s" % e)
        return ""

    async def _shuushuu_find_tag(
        self, session: aiohttp.ClientSession, headers: dict,
        query: str, source: str, tag_type: int | None
    ) -> int | None:
        """查 shuushuu tag_id"""
        try:
            url = f"https://e-shuushuu.net/api/v1/tags/?search={quote(query)}&limit=20"
            async with session.get(
                url,
                headers={**headers, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
            tags = data.get("tags", [])
            if not tags:
                return None

            source_low = source.strip().lower()
            query_low  = query.strip().lower()

            # 精确匹配
            for t in tags:
                if tag_type is not None and t.get("type") != tag_type:
                    continue
                if t["title"].lower() == query_low:
                    logger.info("[shuushuu] 精确命中 tag=%r id=%d" % (t["title"], t["tag_id"]))
                    return t["tag_id"]

            # source 辅助过滤
            if source_low:
                for t in tags:
                    if tag_type is not None and t.get("type") != tag_type:
                        continue
                    haystack = (t["title"] + " " + (t.get("desc") or "")).lower()
                    if source_low in haystack:
                        logger.info("[shuushuu] source命中 tag=%r id=%d" % (t["title"], t["tag_id"]))
                        return t["tag_id"]

            # usage_count 最高
            filtered = [t for t in tags if tag_type is None or t.get("type") == tag_type]
            if filtered:
                best = max(filtered, key=lambda t: t.get("usage_count", 0))
                logger.info("[shuushuu] usage最高 tag=%r id=%d" % (best["title"], best["tag_id"]))
                return best["tag_id"]
        except Exception as e:
            logger.warning("[shuushuu] tag查询失败: %s" % e)
        return None

    async def _shuushuu_fetch_images(
        self, session: aiohttp.ClientSession, headers: dict,
        char_tag_id: int, source_tag_id: int | None, count: int
    ) -> list[bytes]:
        """shuushuu 搜图并下载"""
        images = []
        try:
            search_tag_id = source_tag_id if source_tag_id else char_tag_id
            url = f"https://e-shuushuu.net/api/v1/images/?tags={search_tag_id}&limit={min(count * 6, 40)}"
            async with session.get(
                url,
                headers={**headers, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    logger.warning("[shuushuu] 搜图 HTTP %d" % r.status)
                    return []
                data = await r.json()

            all_images = data.get("images", [])
            logger.info("[shuushuu] 搜图 tag_id=%d 返回%d条" % (search_tag_id, len(all_images)))

            # 有 source_tag 时过滤含 char_tag_id 的图
            if source_tag_id:
                all_images = [
                    img for img in all_images
                    if any(
                        (t.get("tag_id") if isinstance(t, dict) else t) == char_tag_id
                        for t in (img.get("tags") or [])
                    )
                ]
                logger.info("[shuushuu] 过滤后含角色tag的%d条" % len(all_images))

            random.shuffle(all_images)
            async with aiohttp.ClientSession() as dl:
                for img in all_images:
                    if len(images) >= count:
                        break
                    fn  = img.get("filename", "")
                    ext = img.get("ext", "jpg")
                    if not fn:
                        continue
                    img_url = f"https://e-shuushuu.net/images/{fn}.{ext}"
                    try:
                        async with dl.get(
                            img_url,
                            headers={"User-Agent": headers["User-Agent"]},
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as r:
                            if r.status == 200:
                                img_data = await r.read()
                                if len(img_data) > 10 * 1024:
                                    images.append(img_data)
                    except Exception:
                        continue
        except Exception as e:
            logger.error("[shuushuu] 下载图片失败: %s" % e)
        return images

    async def _gelbooru_fetch(self, query: str, count: int) -> list[bytes]:
        """Gelbooru 拉图，仅取 rating:general"""
        images = []
        try:
            params = {
                "page": "dapi", "s": "post", "q": "index", "json": 1,
                "tags": f"{query} rating:general",
                "limit": min(count * 4, 40),
            }
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://gelbooru.com/index.php",
                    params=params,
                    headers={"User-Agent": "astrbot_plugin_animewifex/1.0"},
                    cookies={"fringeBenefits": "yep"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        logger.warning("[添老婆][Gelbooru] HTTP %d query=%r" % (r.status, query))
                        return []
                    data = await r.json()
            posts = data.get("post") or []
            logger.info("[添老婆][Gelbooru] query=%r 返回%d条" % (query, len(posts)))
            random.shuffle(posts)
            async with aiohttp.ClientSession() as s:
                for post in posts:
                    if len(images) >= count:
                        break
                    url = post.get("sample_url") or post.get("file_url")
                    if not url:
                        continue
                    if url.rsplit(".", 1)[-1].lower() not in ("jpg", "jpeg", "png", "webp"):
                        continue
                    try:
                        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                            if r.status == 200:
                                images.append(await r.read())
                    except Exception:
                        continue
        except Exception as e:
            logger.error("[添老婆] Gelbooru 拉图失败: %s" % e)
        return images

    async def _yandere_fetch(self, query: str, count: int) -> list[bytes]:
        """Yande.re 拉图，仅取 rating:safe"""
        images = []
        try:
            params = {"tags": f"{query} rating:s", "limit": min(count * 4, 40), "page": 1}
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://yande.re/post.json",
                    params=params,
                    headers={"User-Agent": "astrbot_plugin_animewifex/1.0"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        logger.warning("[添老婆][Yande.re] HTTP %d query=%r" % (r.status, query))
                        return []
                    posts = await r.json()
            logger.info("[添老婆][Yande.re] query=%r 返回%d条" % (query, len(posts)))
            random.shuffle(posts)
            async with aiohttp.ClientSession() as s:
                for post in posts:
                    if len(images) >= count:
                        break
                    url = post.get("sample_url") or post.get("jpeg_url") or post.get("file_url")
                    if not url:
                        continue
                    if url.rsplit(".", 1)[-1].lower() not in ("jpg", "jpeg", "png", "webp"):
                        continue
                    try:
                        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                            if r.status == 200:
                                images.append(await r.read())
                    except Exception:
                        continue
        except Exception as e:
            logger.error("[添老婆] Yande.re 拉图失败: %s" % e)
        return images

    async def _getchu_fetch(self, source_query: str, count: int) -> list[bytes]:
        """Getchu 官方立绘抓取：搜索作品页，提取立绘/sample大图。
        
        流程：搜索页找商品ID → 商品页抓立绘URL → 下载过滤小图
        立绘通常在 bodypaint/chara/sample 路径，20KB以上才算有效图。
        """
        images = []
        try:
            from curl_cffi.requests import AsyncSession

            search_url = (
                "https://www.getchu.com/php/search.phtml"
                f"?search_keyword={quote(source_query)}&genre=pc_soft&search=search"
            )
            async with AsyncSession() as s:
                # 1. 搜索作品，取第一个结果的商品ID
                r = await s.get(
                    search_url,
                    impersonate="chrome120",
                    headers={"Accept-Language": "ja,en;q=0.9", "Referer": "https://www.getchu.com/"},
                    timeout=20,
                    allow_redirects=True,
                )
                if r.status_code != 200:
                    logger.warning("[添老婆][Getchu] 搜索HTTP %d" % r.status_code)
                    return []

                product_ids = re.findall(r"soft\.phtml\?id=(\d+)", r.text)
                if not product_ids:
                    logger.info("[添老婆][Getchu] 未找到作品: %r" % source_query)
                    return []
                pid = product_ids[0]
                logger.info("[添老婆][Getchu] 找到商品ID: %s" % pid)

                # 2. 商品页抓立绘/sample图URL
                r2 = await s.get(
                    f"https://www.getchu.com/soft.phtml?id={pid}&gc=gc",
                    impersonate="chrome120",
                    headers={"Accept-Language": "ja,en;q=0.9", "Referer": "https://www.getchu.com/"},
                    timeout=20,
                    allow_redirects=True,
                )
                if r2.status_code != 200:
                    return []

                page = r2.text
                # 抓所有 getchu 域名下的图片，包含 bodypaint/chara/sample/brandnew 关键路径
                raw_urls = re.findall(
                    r'["\']((https?:)?//(?:www|img)\.getchu\.com/[^"\']+\.(?:jpg|png))["\']',
                    page, re.I
                )
                seen: set = set()
                full_urls = []
                for groups in raw_urls:
                    u = groups[0]
                    full = ("https:" + u) if u.startswith("//") else u
                    # 优先立绘相关路径
                    is_chara = any(k in full.lower() for k in ("bodypaint", "chara", "sample", "brandnew"))
                    if full not in seen and is_chara:
                        seen.add(full)
                        full_urls.append(full)
                # 补充非立绘路径（兜底）
                for groups in raw_urls:
                    u = groups[0]
                    full = ("https:" + u) if u.startswith("//") else u
                    if full not in seen:
                        seen.add(full)
                        full_urls.append(full)

                logger.info("[添老婆][Getchu] 找到图片URL %d个" % len(full_urls))

            # 3. 下载，过滤小图（< 20KB 视为缩略图）
            async with aiohttp.ClientSession() as sess:
                for url in full_urls:
                    if len(images) >= count:
                        break
                    try:
                        async with sess.get(
                            url,
                            headers={"Referer": "https://www.getchu.com/"},
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                if len(data) > 20 * 1024:
                                    images.append(data)
                    except Exception:
                        continue
        except Exception as e:
            logger.error("[添老婆] Getchu 拉图失败: %s" % e)
        return images

    async def _vndb_fetch(self, char_name: str, source: str, count: int) -> list[bytes]:
        """VNDB API 角色图抓取。
        
        使用 VNDB HTTP API v2（无需认证）：
        POST https://api.vndb.org/kana/character
        按角色名模糊搜索，可选用 vn 过滤缩小范围，取 image.url 下载。
        图片分辨率较低（通常 256x368），但冷门 galgame 角色在此覆盖最全。
        """
        images = []
        try:
            # 构建查询 filter：角色名搜索，可选附加作品名过滤
            # VNDB filter 语法: ["and", ["search", "=", name], ...]
            filters = ["search", "=", char_name]

            payload = {
                "filters": filters,
                "fields": "name, image.url, image.sexual, vns.title",
                "sort": "searchrank",
                "results": min(count * 4, 20),
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.vndb.org/kana/character",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "astrbot_plugin_animewifex/1.0",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        logger.warning("[添老婆][VNDB] HTTP %d char=%r" % (r.status, char_name))
                        return []
                    data = await r.json()

            results = data.get("results") or []
            logger.info("[添老婆][VNDB] char=%r 返回%d条" % (char_name, len(results)))

            # 过滤：sexual >= 2 (explicit) 的图跳过；优先匹配作品名
            def _score(item: dict) -> int:
                sc = 0
                vns = item.get("vns") or []
                if source:
                    for vn in vns:
                        if source.lower() in (vn.get("title") or "").lower():
                            sc += 10
                            break
                img = item.get("image") or {}
                if img.get("url"):
                    sc += 1
                return sc

            results = [r for r in results if (r.get("image") or {}).get("url")]
            results = [r for r in results if ((r.get("image") or {}).get("sexual") or 0) < 2]
            results.sort(key=_score, reverse=True)

            async with aiohttp.ClientSession() as s:
                for item in results:
                    if len(images) >= count:
                        break
                    url = (item.get("image") or {}).get("url")
                    if not url:
                        continue
                    try:
                        async with s.get(
                            url,
                            headers={"User-Agent": "astrbot_plugin_animewifex/1.0"},
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as resp:
                            if resp.status == 200:
                                data_bytes = await resp.read()
                                if len(data_bytes) > 5 * 1024:  # VNDB图较小，5KB起步即可
                                    images.append(data_bytes)
                    except Exception:
                        continue
        except Exception as e:
            logger.error("[添老婆] VNDB 拉图失败: %s" % e)
        return images

    async def _dlsite_cover_fetch(self, source_query: str, count: int) -> list[bytes]:
        """DLsite 封面图终极保底：按作品名搜索，抓封面图"""
        images = []
        try:
            from curl_cffi.requests import AsyncSession

            async def _get_cover_urls(site: str) -> list[str]:
                url = (
                    f"https://www.dlsite.com/{site}/fsr/=/language/jp"
                    f"/keyword/{quote(source_query)}/order/trend/per_page/5"
                )
                async with AsyncSession() as sess:
                    r = await sess.get(
                        url, impersonate="chrome120",
                        headers={"Accept-Language": "ja,en;q=0.9", "Referer": "https://www.dlsite.com/"},
                        timeout=20, allow_redirects=True,
                    )
                    if r.status_code != 200:
                        return []
                    imgs = re.findall(r'src="(//img\.dlsite\.jp/[^"]+\.jpg)"', r.text)
                    return ["https:" + u for u in imgs[:count * 2]]

            cover_urls = []
            for site in ("maniax", "girls"):
                cover_urls.extend(await _get_cover_urls(site))
            # 去重
            seen: set = set()
            cover_urls = [u for u in cover_urls if not (u in seen or seen.add(u))]  # type: ignore
            logger.info("[添老婆][DLsite封面] query=%r 封面URL %d个" % (source_query, len(cover_urls)))

            async with aiohttp.ClientSession() as s:
                for url in cover_urls:
                    if len(images) >= count:
                        break
                    try:
                        async with s.get(
                            url,
                            headers={"Referer": "https://www.dlsite.com/"},
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as r:
                            if r.status == 200:
                                images.append(await r.read())
                    except Exception:
                        continue
        except Exception as e:
            logger.error("[添老婆] DLsite封面拉图失败: %s" % e)
        return images


    async def _pixiv_fetch(self, query: str, count: int) -> list[bytes]:
        """Pixiv 搜索角色图片（全年龄）"""
        images = []
        try:
            from pixivpy3 import AppPixivAPI
            api = AppPixivAPI()
            await asyncio.get_event_loop().run_in_executor(
                None, api.auth, None, None, self.pixiv_refresh_token
            )
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: api.search_illust(
                    query,
                    search_target="partial_match_for_tags",
                    filter="for_ios",
                )
            )
            posts = result.illusts or []
            # 只取全年龄
            posts = [p for p in posts if getattr(p, "x_restrict", 1) == 0]
            random.shuffle(posts)
            logger.info("[添老婆][Pixiv] query=%r 返回%d条(全年龄)" % (query, len(posts)))

            async with aiohttp.ClientSession() as s:
                for post in posts:
                    if len(images) >= count:
                        break
                    try:
                        url = post.image_urls.get("large") or post.image_urls.get("medium")
                        if not url:
                            continue
                        async with s.get(
                            url,
                            headers={"Referer": "https://www.pixiv.net/"},
                            timeout=aiohttp.ClientTimeout(total=20)
                        ) as r:
                            if r.status == 200:
                                images.append(await r.read())
                    except Exception:
                        continue
        except ImportError:
            logger.warning("[添老婆] pixivpy3 未安装，跳过 Pixiv 拉图")
        except Exception as e:
            logger.error(f"[添老婆] Pixiv 拉图失败: {e}")
        return images

    async def _konachan_fetch(self, query: str, count: int) -> list[bytes]:
        """Konachan safe 图片拉取"""
        images = []
        try:
            params = {"tags": f"{query} rating:safe", "limit": count * 3, "page": 1}
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://konachan.com/post.json",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        return []
                    posts = await r.json()

            random.shuffle(posts)
            for post in posts:
                if len(images) >= count:
                    break
                url = post.get("sample_url") or post.get("jpeg_url") or post.get("file_url")
                if not url:
                    continue
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                            if r.status == 200:
                                images.append(await r.read())
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[添老婆] Konachan 拉图失败: {e}")
        return images

    async def _danbooru_fetch(self, query: str, count: int) -> list[bytes]:
        """Danbooru safe 图片拉取（无需认证，最多100条）"""
        images = []
        try:
            params = {"tags": f"{query} rating:general", "limit": min(count * 3, 20), "page": 1}
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://danbooru.donmai.us/posts.json",
                    params=params,
                    headers={"User-Agent": "astrbot_plugin_animewifex/1.0"},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        logger.warning("[添老婆][Danbooru] HTTP %d query=%r" % (r.status, query))
                        return []
                    posts = await r.json()

            logger.info("[添老婆][Danbooru] query=%r 返回%d条" % (query, len(posts)))
            random.shuffle(posts)
            for post in posts:
                if len(images) >= count:
                    break
                url = post.get("large_file_url") or post.get("file_url")
                if not url:
                    continue
                ext = url.rsplit(".", 1)[-1].lower()
                if ext not in ("jpg", "jpeg", "png", "webp"):
                    continue
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                            if r.status == 200:
                                images.append(await r.read())
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[添老婆] Danbooru 拉图失败: {e}")
        return images

    def _get_img_dir(self, source: str) -> str:
        """根据 list.txt 判断作品应放哪个 img 目录（img1 已满，强制不再写入）"""
        list_txt = os.path.join(CONFIG_DIR, "list_cache.txt")

        if os.path.exists(list_txt):
            try:
                with open(list_txt, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        m = re.match(r'^(img\d+)/', line)
                        if not m:
                            continue
                        img_dir = m.group(1)
                        if img_dir == "img1":
                            continue  # img1 已满，强制跳过
                        rest = line[len(img_dir) + 1:]
                        if "!" in rest:
                            src = rest.split("!", 1)[0]
                            if src == source:
                                return img_dir
            except Exception:
                pass
        return random.choice(["img2", "img3"])

    @staticmethod
    def _detect_img_ext(data: bytes) -> str:
        """根据 magic bytes 检测图片格式，返回扩展名（含点）"""
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return ".png"
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return ".webp"
        if data[:3] == b'GIF':
            return ".gif"
        if data[:4] == b'\x00\x00\x01\x00':
            return ".ico"
        if data[:2] == b'BM':
            return ".bmp"
        # JPEG: FFD8FF（覆盖 jpg 和 jpeg）
        if data[:3] == b'\xff\xd8\xff':
            return ".jpg"
        # 默认当 jpg
        return ".jpg"

    async def _create_github_pr_empty(self, source: str, char_name: str, img_dir: str) -> str | None:
        """创建不含图片的空 PR，供手动上传"""
        token = self.github_token
        repo = self.github_repo
        branch = self.github_branch
        if not token:
            return None

        safe_source = re.sub(r'[\\/:*?"<>|]', '_', source)
        safe_char = re.sub(r'[\\/:*?"<>|]', '_', char_name)
        filename = f"{img_dir}/{safe_source}!{safe_char}.jpg" if safe_source else f"{img_dir}/{safe_char}.jpg"

        # 用英文名生成分支名和占位文件名，避免韩文/中文等非ASCII字符导致GitHub API报错
        en_cache = load_json(EN_CACHE_FILE)
        en_name = en_cache.get(char_name, {}).get("en", "") if isinstance(en_cache.get(char_name), dict) else en_cache.get(char_name, "")
        branch_char = en_name if en_name else char_name
        safe_branch_char = re.sub(r'[^a-zA-Z0-9]', '-', branch_char[:20])
        pr_branch = f"add-char-{safe_branch_char}-{random.randint(1000,9999)}"
        # 占位文件名也用英文，避免非ASCII
        placeholder_name = f".placeholder_{safe_branch_char}"

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        base_url = f"https://api.github.com/repos/{repo}"
        logger.info(f"[添老婆] 开始创建空PR: branch={pr_branch} filename={filename}")

        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(f"{base_url}/git/ref/heads/{branch}") as r:
                    if r.status != 200:
                        logger.error(f"[添老婆] 步骤1 获取SHA失败: {r.status} {await r.text()}")
                        return None
                    main_sha = (await r.json())["object"]["sha"]
                    logger.info(f"[添老婆] 步骤1 获取SHA成功: {main_sha[:8]}")

                async with s.post(f"{base_url}/git/refs", json={
                    "ref": f"refs/heads/{pr_branch}",
                    "sha": main_sha,
                }) as r:
                    if r.status not in (200, 201):
                        logger.error(f"[添老婆] 步骤2 创建分支失败: {r.status} {await r.text()}")
                        return None
                    logger.info(f"[添老婆] 步骤2 创建分支成功: {pr_branch}")

                # 上传一个占位文件
                import base64 as b64
                placeholder = b64.b64encode(
                    f"请在此目录上传图片：{filename}".encode()
                ).decode()
                async with s.put(f"{base_url}/contents/{img_dir}/{placeholder_name}", json={
                    "message": f"Add: {source}!{char_name} (需手动上传图片)",
                    "content": placeholder,
                    "branch": pr_branch,
                }) as r:
                    if r.status not in (200, 201):
                        logger.error(f"[添老婆] 步骤3 上传占位文件失败: {r.status} {await r.text()}")
                        return None
                    logger.info(f"[添老婆] 步骤3 上传占位文件成功")

                # 空PR也预写 list.txt，上传图片后 merge 就完成
                list_path = "list.txt"
                list_sha = None
                list_content_old = ""
                import base64 as _b64
                async with s.get(f"{base_url}/contents/{list_path}", params={"ref": pr_branch}) as r:
                    if r.status == 200:
                        data = await r.json()
                        list_sha = data.get("sha")
                        list_content_old = _b64.b64decode(data["content"].replace("\n","")).decode("utf-8")
                    logger.info(f"[添老婆] 步骤4 读取list.txt: status={r.status}")

                list_content_new = list_content_old.rstrip("\n") + "\n" + filename + "\n"
                lines_sorted = sorted(set(l for l in list_content_new.splitlines() if l.strip()))
                list_content_new = "\n".join(lines_sorted) + "\n"
                list_encoded = _b64.b64encode(list_content_new.encode("utf-8")).decode()
                put_body = {
                    "message": f"Auto: update list.txt for {source}!{char_name}",
                    "content": list_encoded,
                    "branch": pr_branch,
                }
                if list_sha:
                    put_body["sha"] = list_sha
                async with s.put(f"{base_url}/contents/{list_path}", json=put_body) as r:
                    if r.status not in (200, 201):
                        logger.error(f"[添老婆] 步骤5 更新list.txt失败: {r.status} {await r.text()}")
                    else:
                        logger.info(f"[添老婆] 步骤5 更新list.txt成功")

                body = (
                    f"新增角色：{source} - {char_name}\n\n"
                    f"⚠️ 自动拉图失败，请手动上传图片到分支 `{pr_branch}` 的以下路径：\n"
                    f"- `{filename}`\n\n"
                    f"上传图片后直接 merge 即可，list.txt 已预先更新。"
                )
                async with s.post(f"{base_url}/pulls", json={
                    "title": f"Add: {source}!{char_name}",
                    "head": pr_branch,
                    "base": branch,
                    "body": body,
                }) as r:
                    if r.status not in (200, 201):
                        logger.error(f"[添老婆] 步骤6 创建PR失败: {r.status} {await r.text()}")
                        return None
                    pr_url = (await r.json()).get("html_url")
                    logger.info(f"[添老婆] 步骤6 PR创建成功: {pr_url}")
                    return pr_url
        except Exception as e:
            logger.error(f"[添老婆] 创建空PR异常: {e}", exc_info=True)
            return None

    async def _create_github_pr(
        self, source: str, char_name: str, img_dir: str, images: list[bytes]
    ) -> str | None:
        """通过 GitHub API 创建 PR"""
        import base64 as b64
        token = self.github_token
        repo = self.github_repo
        branch = self.github_branch

        if not token or not images:
            return None

        # 生成文件名（同一角色多张图加 _2 _3 后缀）
        safe_source = re.sub(r'[\\/:*?"<>|]', '_', source)
        safe_char = re.sub(r'[\\/:*?"<>|]', '_', char_name)
        base = f"{safe_source}!{safe_char}" if safe_source else safe_char
        file_names = []
        for i, img_data in enumerate(images):
            suffix = "" if i == 0 else f"_{i + 1}"
            ext = self._detect_img_ext(img_data)
            file_names.append(f"{img_dir}/{base}{suffix}{ext}")
        logger.info("[添老婆] 生成文件名: %s" % file_names)

        pr_branch = f"add-char-{re.sub(r'[^a-zA-Z0-9]', '-', char_name[:20])}-{random.randint(1000, 9999)}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        base_url = f"https://api.github.com/repos/{repo}"

        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                # 1. 获取 main SHA
                async with s.get(f"{base_url}/git/ref/heads/{branch}") as r:
                    if r.status != 200:
                        logger.error(f"[添老婆] 获取分支 SHA 失败: {r.status}")
                        return None
                    ref_data = await r.json()
                main_sha = ref_data["object"]["sha"]

                # 2. 创建新分支
                async with s.post(f"{base_url}/git/refs", json={
                    "ref": f"refs/heads/{pr_branch}",
                    "sha": main_sha,
                }) as r:
                    if r.status not in (200, 201):
                        logger.error(f"[添老婆] 创建分支失败: {r.status}")
                        return None

                # 3. 上传图片
                for fname, img_data in zip(file_names, images):
                    img_content = b64.b64encode(img_data).decode()
                    async with s.put(f"{base_url}/contents/{fname}", json={
                        "message": f"Add: {source}!{char_name}",
                        "content": img_content,
                        "branch": pr_branch,
                    }) as r:
                        if r.status not in (200, 201):
                            logger.error(f"[添老婆] 上传图片失败: {fname} {r.status}")
                            await s.delete(f"{base_url}/git/refs/heads/{pr_branch}")
                            return None

                # 4. 更新 list.txt（直接写入，不依赖 Actions）
                list_path = "list.txt"
                list_sha = None
                list_content_old = ""
                async with s.get(f"{base_url}/contents/{list_path}", params={"ref": pr_branch}) as r:
                    if r.status == 200:
                        data = await r.json()
                        list_sha = data.get("sha")
                        import base64 as _b64
                        list_content_old = _b64.b64decode(data["content"].replace("\n","")).decode("utf-8")

                new_entries = "\n".join(file_names)
                list_content_new = list_content_old.rstrip("\n") + "\n" + new_entries + "\n"
                # 去重排序
                lines_sorted = sorted(set(l for l in list_content_new.splitlines() if l.strip()))
                list_content_new = "\n".join(lines_sorted) + "\n"
                list_encoded = b64.b64encode(list_content_new.encode("utf-8")).decode()

                put_body = {
                    "message": f"Auto: update list.txt for {source}!{char_name}",
                    "content": list_encoded,
                    "branch": pr_branch,
                }
                if list_sha:
                    put_body["sha"] = list_sha
                async with s.put(f"{base_url}/contents/{list_path}", json=put_body) as r:
                    if r.status not in (200, 201):
                        logger.error(f"[添老婆] 更新 list.txt 失败: {r.status}")
                        # 不中断，继续创建 PR

                # 5. 创建 PR
                body_lines = [
                    f"新增角色图片：{source} - {char_name}", "",
                ] + [f"- `{f}`" for f in file_names] + [
                    "", "merge 后 list.txt 已同步更新，无需额外操作。"
                ]
                async with s.post(f"{base_url}/pulls", json={
                    "title": f"Add: {source}!{char_name}",
                    "head": pr_branch,
                    "base": branch,
                    "body": "\n".join(body_lines),
                }) as r:
                    if r.status not in (200, 201):
                        logger.error(f"[添老婆] 创建 PR 失败: {r.status}")
                        return None
                    pr_data = await r.json()
                    return pr_data.get("html_url")
        except Exception as e:
            logger.error(f"[添老婆] GitHub API 异常: {e}")
            return None

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
        config_locks.clear()
        records.clear()
        swap_requests.clear()
        ntr_statuses.clear()
        add_sessions.clear()
        pending_queue.clear()