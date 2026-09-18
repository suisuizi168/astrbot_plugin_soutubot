# astrbot_plugin_soutubot

AstrBot 的搜图Bot酱插件。它只在明确触发时上传当前消息或引用消息中的图片，不监听普通图片消息。

## 功能

- `/找本` 与图片放在同一条消息中。
- 引用一条图片消息后发送 `/找本`。
- 引用链缺少内嵌图片时，通过 AstrBot 引用解析器回查 OneBot 消息。
- 空白名单时全部会话可用；填写白名单后只允许指定群或私聊用户。
- 异步后台搜索，不让指令长时间占用消息处理器。
- 兼容搜图Bot酱当前与旧版 JSON 返回结构。

## 安装

将仓库放入 AstrBot 的 `data/plugins/astrbot_plugin_soutubot`，安装依赖并重载插件：

```text
aiohttp>=3.9,<4
Pillow>=10,<13
```

也可以通过 AstrBot WebUI 从 Git 仓库安装。

## 使用

```text
/找本 [图片]
```

或者引用一条图片消息后发送：

```text
/找本
```

一条消息有多张图片时只处理第一张。仅展示相似度不低于 80% 的结果，并附带每条结果的缩略图；不输出搜图网站的完整结果页地址。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `whitelist` | `[]` | 空列表允许全部；群聊填群号，私聊填用户 ID |
| `strict_mode` | `false` | `false` 使用 1.2，`true` 使用严格模式 1.4 |
| `timeout_seconds` | `300` | 搜索总超时 |
| `cooldown_seconds` | `30` | 单用户冷却 |
| `max_concurrency` | `2` | 最大并发搜索数 |
| `max_pending_tasks` | `20` | 最大后台排队任务数 |
| `top_k` | `25` | 网站检索候选数 |
| `max_results` | `3` | 聊天内展示条数 |

相似度展示阈值固定为 80%。如果没有结果达到阈值，机器人会明确提示未找到满足条件的结果。

## 网络行为

插件先访问 `https://soutubot.moe/` 建立会话，再向 `https://soutubot.moe/api/search` 提交 multipart 表单。遇到 Cloudflare 拒绝时只刷新一次正常会话，不包含验证码、浏览器自动化或其他绕过逻辑。

## 开发测试

```powershell
python -m unittest discover -s tests -v
```
