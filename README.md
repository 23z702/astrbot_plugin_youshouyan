# 有兽焉追番（astrbot_plugin_youshouyan）

AstrBot 插件：发送 `/看 有兽焉第12话`，从站点媒体接口取回临时播放直链并以视频消息发到群里。

数据来源：`/api/media.php`（`list` 检索 + `video` 换取限时直链），鉴权用 `X-API-Key`。

## 支持的说法

| 输入 | 结果 |
| --- | --- |
| `/看 有兽焉第12话` | 第 12 话 |
| `/看 有兽焉 第12集` | 第 12 话 |
| `/看 有兽焉 12` | 第 12 话 |
| `/看 有兽焉第十二话` | 第 12 话 |
| `/看 有兽焉第1-3话` | 第 1、2、3 话（上限 `max_range`） |

指令别名：`看有兽焉`、`youshouyan`、`kan`；另有 `/有兽焉帮助` 和 `/有兽焉自检`（调用 `action=info` 显示密钥名、作用域、有效期、调用次数，排障先跑这个）。
闲聊里顺口带「看」字（例如「看这个」）不会触发，只有带数字或用 `/看` 才响应。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/23z702/astrbot_plugin_youshouyan.git
```

也可以下载压缩包解压到 `AstrBot/data/plugins/astrbot_plugin_youshouyan/`——目录名必须是 `astrbot_plugin_youshouyan`，带连字符 AstrBot 加载不了。

重载插件后在管理面板启用，再填下面的配置。

## 必填配置

| 配置 | 说明 |
| --- | --- |
| `api_base_url` | `https://你的域名/api/media.php`（中文域名会自动转 punycode，填中文或 `xn--` 形式都行） |
| `api_key` | `pk_` 开头 51 字符，用户中心 → 视频 API → 新建密钥（明文只显示一次） |

密钥用 `X-API-Key` 请求头发送，不依赖 nginx 透传 `Authorization`。
若站点只认 `Authorization: Bearer`，把 `api_key_header` 改成 `Authorization`、`api_key_prefix` 改成 `Bearer `（含空格）。

⚠️ 密钥等于「以你的身份读文件」：只填在 AstrBot 配置里，不要进前端、公开仓库或截图。

## 工作流程

```
/看 有兽焉第12话
  ├─ GET ?action=list&q=有兽焉&page=1&page_size=100   ← 结果缓存 cache_ttl 秒，自动翻页
  ├─ 用文件名匹配第 12 话，多个命中取最像的、再取上传时间最新的
  └─ GET ?action=video&id=123&ttl=7200                ← 直链在有效期内复用
       └─ 发视频消息（超过 max_send_size_mb 则改为发直链文本）
```

### 文件名匹配规则

| 文件名 | 识别为 | 说明 |
| --- | --- | --- |
| `有兽焉 第12话 1080p.mp4` | 第 12 话 | 「第N话/集/期」最可信 |
| `有兽焉.S01E05.mkv` | 第 5 话 | `E05`/`EP05` 次之，季号 `S01` 会被剔除 |
| `有兽焉 [12].mp4` | 第 12 话 | 括号数字 |
| `有兽焉 1080p 第2话.mp4` | 第 2 话 | 分辨率/编码等噪声先剔除，不会被当成话数 |
| `有兽焉 第12话 预告.mp4` | 第 12 话 | 但会扣分，同一话有正片时优先正片 |
| `有兽焉 主题曲.mp3` | 不匹配 | 非 video/ 的 mime 直接过滤 |

同一话命中多个文件时，取匹配可信度最高的；同分再取 `create_time` 最新的。

### 发送策略

- 文件 ≤ `max_send_size_mb`（默认 100MB）：发视频消息（文本+视频一条链）；
- 超过上限或开启 `link_only`：发文本，含文件名、大小和直链，并说明为什么没发视频；
- 视频消息发送失败会自动降级为直链文本，不会哑掉。

## 其余配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `play_ttl` | 7200 | 直链有效期（接口允许 60~86400） |
| `cache_ttl` | 300 | 文件列表缓存；查不到话数时会自动强制刷新一次 |
| `page_size` / `max_pages` | 100 / 5 | 最多检索 500 个文件 |
| `max_episode` | 0 | 话数上限，0 不限；填了就本地拦截，不浪费请求 |
| `max_range` | 3 | 一次最多查几话 |
| `timeout` | 15 | 单次请求超时 |
| `extra_headers` | `{}` | 附加请求头（如 Referer） |

## 错误提示

接口错误码会翻成中文，401/403/429/503 额外给出处理建议：

| code | 提示 |
| --- | --- |
| 401 | 密钥无效/已撤销/已过期 → 更新插件配置 |
| 403 | 作用域不足（缺 `media`） |
| 404 | 该话文件不存在（或不属于这个账号） |
| 415 | 文件不是可播放视频 |
| 429 | 触发限流（按 IP，15 分钟 30 次） |
| 503 | 服务端没跑 `install/migrate.php` |

## 注意

- `/api/media.php` 返回的是带时效的凭证，站点若挂了 CDN，记得对该路径设置为不缓存；
- 直链本身即凭证，拿到的人都能看，直到过期，别往公开页面贴；
- 想长时间可用就调大 `play_ttl`，而不是去做永久公开直链。

## 自测

```bash
python tests/smoke_test.py
```

用假 astrbot 模块 + 本地模拟 `/api/media.php`，覆盖 7 个指令场景、文件名匹配、缓存命中、401、未配置、话数上限。需要 `aiohttp`。

## 结构

```
main.py              指令处理、消息发送
parser.py            指令解析（中文数字、范围）
api.py               接口请求、文件名匹配、缓存
tests/smoke_test.py  离线冒烟测试
_conf_schema.json    配置面板结构
metadata.yaml        插件元信息
```

## 许可

[MIT](LICENSE)
