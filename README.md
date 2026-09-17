# astrbot_plugin_soutubot

AstrBot 的搜图Bot酱插件。它只在明确触发时上传当前消息或引用消息中的图片，不监听普通图片消息。

## 功能

- `/搜本子` 与图片放在同一条消息中。
- 引用一条图片消息后发送 `/搜本子`。
- 可选 LLM 工具 `search_doujin_by_current_image`。
- 空白名单时全部会话可用；填写白名单后只允许指定群或私聊用户。
- 异步后台搜索，不让固定指令长时间占用消息处理器。
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
/搜本子 [图片]
```

或者引用一条图片消息后发送：

```text
/搜本子
```

一条消息有多张图片时只处理第一张。返回消息展示配置数量的结果，并附带完整网页结果链接。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `whitelist` | `[]` | 空列表允许全部；群聊填群号，私聊填用户 ID |
| `strict_mode` | `false` | `false` 使用 1.2，`true` 使用严格模式 1.4 |
| `enable_llm_tool` | `true` | 启用窄用途 LLM 工具 |
| `timeout_seconds` | `300` | 搜索总超时 |
| `cooldown_seconds` | `30` | 单用户冷却 |
| `max_concurrency` | `2` | 最大并发搜索数 |
| `max_pending_tasks` | `20` | 最大后台排队任务数 |
| `top_k` | `25` | 网站检索候选数 |
| `max_results` | `3` | 聊天内展示条数 |

## LLM 工具

支持 Function Calling 的模型可以调用 `search_doujin_by_current_image`。工具仅从当前事件或引用消息中读取图片，并再次执行白名单与冷却校验；它不接受任意 URL 或本地路径。

固定 `/搜本子` 指令不依赖 LLM，即使模型不支持工具调用也可以使用。

## 网络行为

插件先访问 `https://soutubot.moe/` 建立会话，再向 `https://soutubot.moe/api/search` 提交 multipart 表单。遇到 Cloudflare 拒绝时只刷新一次正常会话，不包含验证码、浏览器自动化或其他绕过逻辑。

## 开发测试

```powershell
python -m unittest discover -s tests -v
```
