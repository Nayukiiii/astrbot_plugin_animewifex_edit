# astrbot_plugin_animewifex_edit

AstrBot 群聊二次元老婆插件。核心玩法是每日抽老婆，同时提供换老婆、牛老婆、交换老婆、要本子、图鉴留存、群友添老婆审核和 GitHub 图床 PR 上线流程。

这个分支已经做过一次架构拆分：`main.py` 主要负责 AstrBot 事件和消息编排，翻译、角色解析、审核状态、图源拉取、GitHub 发布、留存统计等逻辑放在 `services/` 下，后续维护不需要再往一个大文件里硬塞。

## 功能概览

| 模块 | 说明 |
| --- | --- |
| 每日抽老婆 | 每人每天固定一位老婆，重复抽取返回当天结果 |
| 去重池 | 每个用户记录最近抽过的角色，避免长期重复 |
| 留存图鉴 | `老婆图鉴`、`今日老婆榜`、`老婆排行`、`图鉴排行` |
| 换老婆 | 每日有限次数重抽，支持锁定角色限制 |
| 牛老婆 | 概率抢走他人今日老婆，可开关 |
| 交换老婆 | 双方同意后互换今日老婆 |
| 业力系统 | 被他人成功重置换老婆后，下次抽老婆有概率触发惩罚图 |
| 要本子 | 根据今日老婆解析角色与作品，搜索 JM / NH / EH / DL 等来源 |
| 添老婆 | 群友提交角色，管理员审核，自动拉图并创建 GitHub PR |
| 翻译缓存 | 角色/作品多语言档案缓存，减少重复 AI 调用 |

## 用户指令

| 指令 | 说明 |
| --- | --- |
| `抽老婆` | 抽取今日老婆 |
| `查老婆 [@用户]` | 查看自己或别人今日老婆 |
| `老婆图鉴` / `我的图鉴` | 查看连续抽取、累计抽取和图鉴进度 |
| `今日老婆榜` | 查看本群今天已抽取列表 |
| `老婆排行` / `连续抽老婆排行` | 查看连续抽老婆排行 |
| `图鉴排行` | 查看本群图鉴收集排行 |
| `要本子` | 用今日老婆搜索相关本子 |
| `换老婆` | 放弃今日老婆并重抽 |
| `牛老婆 @用户` | 尝试抢走对方今日老婆 |
| `交换老婆 @用户` | 发起老婆交换请求 |
| `同意交换 @用户` | 同意交换请求 |
| `拒绝交换 @用户` | 拒绝交换请求 |
| `查看交换请求` | 查看当前交换请求 |
| `添老婆 角色名/作品名` | 搜索并提交新角色 |
| `我的老婆申请` | 查看自己的提交进度 |
| `解析角色 角色名/作品名` | 查看翻译档案和图源搜索名 |

## 管理员指令

| 指令 | 说明 |
| --- | --- |
| `切换ntr开关状态` | 开关本群牛老婆 |
| `重置牛 [@用户]` | 管理员可直接重置牛老婆次数 |
| `重置换 [@用户]` | 管理员可直接重置换老婆次数 |
| `刷新缓存` | 批量刷新角色英文/别名缓存 |
| `重译角色 角色名/作品名` | 清除单个角色翻译缓存并重新解析 |
| `拉取老婆审核` | 私聊查看待审核队列 |
| `通过 <序号>` | 通过申请并进入拉图确认 |
| `通过 <序号> 作品名` | 通过时补充/覆盖作品名 |
| `拒绝 <序号>` | 拒绝申请 |
| `选 N <pid>` | 使用第 N 张候选图创建 PR |
| `确认 <pid>` | 使用全部候选图创建 PR |
| `换图 <pid>` | 重新拉取候选图 |
| `跳过 <pid>` | 创建空 PR，后续手动上传图片 |
| `pr上线 <序号/pid>` | PR merge 后标记上线并通知提交者 |

## 添老婆流程

推荐用户输入 `添老婆 角色名/作品名`，作品名越明确，搜索和拉图越准。

1. 插件先查重，避免重复提交。
2. 角色解析走 Bangumi、AniList、VNDB，优先过滤同作品候选。
3. 如果找不到角色，但用户给了作品名，会直接提交人工审核，不让用户卡死在搜索页。
4. 管理员通过后，插件自动拉候选图并私聊展示。
5. 管理员选择图片后，插件创建 GitHub PR 并更新 `list.txt`。
6. PR merge 后执行 `pr上线 <pid>`，插件通知提交者。

审核状态统一在 `services/review.py`，目前包括：

| 状态 | 含义 |
| --- | --- |
| `need_source` | 待补来源 |
| `pending` | 待审核 |
| `approved` | 已通过，待选图 |
| `image_ready` | 图片待确认 |
| `pr_created` | PR 已创建，待上线 |
| `online` | 已上线 |
| `rejected` | 未通过 |

## 图源策略

拉图逻辑在 `services/image_fetcher.py`，当前策略是“先准、再多”：

1. Pixiv：如果配置了 `pixiv_refresh_token`，优先用角色名 + 作品名搜索。
2. 自定义图源：`extra_image_sources` 可扩展站点。
3. e-shuushuu：有 token 时使用，偏角色准确。
4. Booru 组合标签：先搜 `角色_tag 作品_tag`，减少错角色。
5. VNDB：作为 gal/VN 角色官方图兜底。
6. Booru 纯角色标签：组合标签和 VNDB 不够时再放宽。
7. Getchu / DLsite：只在没有角色图时用作品封面兜底。
8. 候选缩略图：最后 fallback，避免审核流程完全没图。

这个顺序比“直接纯角色名扫 booru”更稳，尤其是同名角色和冷门 gal 角色。

## 配置

常用配置在 `_conf_schema.json` 中维护。关键项如下：

| 配置键 | 说明 |
| --- | --- |
| `need_prefix` | 是否需要前缀或 @Bot 才触发 |
| `image_base_url` | 图片基础 URL |
| `image_list_url` | `list.txt` 直链 |
| `admin_qq` | 管理员 QQ |
| `github_token` | 创建 PR 用的 GitHub token |
| `github_repo` | 图床仓库，如 `owner/repo` |
| `github_branch` | 图床目标分支，默认 `main` |
| `nvidia_api_key` | 翻译/解析角色用 |
| `pixiv_refresh_token` | Pixiv 图源，可选 |
| `shuushuu_access_token` | e-shuushuu 图源，可选 |
| `extra_image_sources` | 自定义图源名称，逗号分隔 |
| `ntr_max` | 每日牛老婆次数 |
| `change_max_per_day` | 每日换老婆次数 |
| `swap_max_per_day` | 每日交换请求次数 |
| `reset_max_uses_per_day` | 每日重置次数 |
| `karma_img1` / `karma_img2` | 业力惩罚图 |
| `karma_base_prob` / `karma_max_prob` | 业力概率 |
| `up_chars` | 单角色 UP，逗号分隔 |
| `up_prob` | 单角色 UP 概率 |
| `up_pool_prob` | 常驻 UP 池概率 |
| `lock_char` | 换老婆锁定角色，逗号分隔 |
| `reset_char` | 抽到后重置去重池的角色 |

## 数据文件

插件数据默认放在 AstrBot 数据目录下：

```text
data/astrbot_plugin_animewifex/config/
```

主要文件：

| 文件 | 说明 |
| --- | --- |
| `records.json` | 每日次数、业力、连续抽取统计 |
| `drawn_pool.json` | 用户去重池和图鉴基础数据 |
| `list_cache.txt` | 图床 `list.txt` 本地缓存 |
| `pending.json` | 添老婆待审核队列 |
| `add_sessions.json` | 添老婆交互会话 |
| `en_cache.json` | 角色翻译档案缓存 |
| `karma_groups.json` | 分群业力/UP 配置 |

## 架构

| 路径 | 职责 |
| --- | --- |
| `main.py` | AstrBot 入口、指令注册、事件响应、消息发送 |
| `hentai_search.py` | 要本子搜索和 AI 角色解析 |
| `karma.py` | 业力、UP、锁定相关规则 |
| `services/translation.py` | 翻译缓存读写与兼容旧格式 |
| `services/character_resolver.py` | Bangumi / AniList / VNDB 角色候选搜索 |
| `services/image_fetcher.py` | Pixiv / e-shuushuu / booru / VNDB / DLsite 拉图 |
| `services/github_publisher.py` | GitHub 分支、文件、`list.txt`、PR 发布 |
| `services/review.py` | 审核状态常量 |
| `services/retention.py` | 连续抽取、图鉴、排行和留存提示 |
| `tools/dry_run_flow.py` | 离线流程自检脚本 |
| `ARCHITECTURE.md` | 拆分规则和后续迁移方向 |

## 本地检查

```powershell
python -m py_compile main.py services\github_publisher.py services\image_fetcher.py services\translation.py services\character_resolver.py services\review.py services\retention.py hentai_search.py karma.py tools\dry_run_flow.py
python tools\dry_run_flow.py
```

`dry_run_flow.py` 不访问外网，会检查翻译缓存、留存统计、审核状态、图源顺序和 GitHub 发布工具的关键路径。

## 部署

常规部署方式：

1. 将仓库内容同步到 AstrBot 插件目录。
2. 确认 `requirements.txt` 或 AstrBot 环境内已有 `aiohttp`、`Pillow`。
3. 重启 AstrBot 容器或重载插件。
4. 用 `python -m py_compile` 或 AstrBot 日志确认插件加载正常。

OCI 上如果使用 Docker 部署，推荐流程是先备份插件目录，再覆盖代码，最后清理 `__pycache__` 并在容器里做编译检查。

## 注意事项

- GitHub token、Pixiv token、NVIDIA API key 不要写进仓库。
- 添老婆建议强制带作品名，能明显降低错角色和找不到图的概率。
- 自动拉图失败不是异常流程，`跳过 <pid>` 会创建空 PR，适合管理员手动补图。
- `list.txt` 是抽老婆和图鉴统计的基础，图片 merge 后要确保路径同步进去。

## License

MIT License. See [LICENSE.txt](LICENSE.txt).
