# 搜图Bot酱找本

调用搜图Bot酱@soutubot，简单的调用工具。

搜图Bot酱@soutubot（网址：[https://soutubot.moe/](https://soutubot.moe/)），可局部搜图 NH 内的本子。本插件只在明确触发时上传当前消息或引用消息中的图片，不监听普通图片消息。

## 功能

- `/找本` 与图片放在同一条消息中。
- 引用一条图片消息后发送 `/找本`。
- 引用链缺少内嵌图片时，通过 AstrBot 引用解析器回查 OneBot 消息。
- 空白名单时全部会话可用；填写白名单后只允许指定群或私聊用户。
- 异步后台搜索，不让指令长时间占用消息处理器。
- 兼容搜图Bot酱当前与旧版 JSON 返回结构。

## 安装

### 方式一：AstrBot 插件市场（上架后推荐）

插件通过审核并上架后，进入 AstrBot WebUI 的 **插件 → 插件市场**，搜索 **搜图Bot酱找本**，点击安装即可。AstrBot 会下载插件，并根据仓库内的 `requirements.txt` 自动处理 Python 依赖；不需要另外对接安装接口，也不需要在 README 中嵌入安装按钮。

> 插件尚未通过市场审核时，市场中不会出现这一安装入口，此时可使用下面的 URL 或 ZIP 安装方式。

### 方式二：通过仓库链接安装

在 AstrBot WebUI 的插件页面点击右下角的 `+`，选择通过 URL 安装，填入：

```text
https://github.com/suisuizi168/astrbot_plugin_soutubot
```

仓库公开后可以直接使用该方式。私有仓库无法由未配置 GitHub 凭据的 AstrBot 实例直接下载。

### 方式三：通过 ZIP 文件安装

在 GitHub 仓库页面选择 **Code → Download ZIP**，然后在 AstrBot WebUI 的插件安装窗口选择文件上传，直接上传下载到的 `.zip` 文件，无需手动解压。

### 方式四：手动安装

在 AstrBot 根目录执行：

```bash
cd data/plugins
git clone https://github.com/suisuizi168/astrbot_plugin_soutubot.git
```

安装完成后，在 WebUI 中重载插件或重启 AstrBot。

正常情况下，AstrBot 会根据 `requirements.txt` 自动安装以下依赖：

```text
aiohttp>=3.9,<4
Pillow>=10,<13
```

如果自动安装依赖失败，请优先在 AstrBot WebUI 的依赖安装功能中安装上述包，以确保依赖进入 AstrBot 实际使用的 Python 环境，而不是系统中的其他 Python 环境。

## 插件市场发布说明

本仓库已经包含市场识别和一键安装所需的主要文件：

- `metadata.yaml`：插件身份、版本、展示名、简介、仓库地址和支持平台。
- `requirements.txt`：安装时需要处理的第三方依赖。
- `_conf_schema.json`：WebUI 可视化配置项。
- `LICENSE` 与本说明文档。

提交市场前还需要完成：

1. 将 GitHub 仓库设为公开，并确认默认分支可匿名访问。
2. 在 AstrBot 插件发布页面登录 AstrBot Cloud 账号并提交仓库。
3. 提交时保证市场记录中的 `author`、`name`、`version` 与 `metadata.yaml` 完全一致。
4. 后续发布新版本时先更新 `metadata.yaml` 中的 `version`，再推送代码并更新市场记录。

市场安装会使用市场记录中的下载地址；未单独提供下载地址时，会直接使用 `metadata.yaml` 中声明的 GitHub 仓库。因此，上架后即可由插件市场一键安装，不需要本插件额外开发安装接口。

## 使用

```text
/找本 [图片]
```

或者引用一条图片消息后发送：

```text
/找本
```

一条消息有多张图片时只处理第一张。默认仅展示相似度不低于 80% 的前 3 条结果，并附带每条结果的预览图、匹配页与详情页地址；这些内容均可在插件配置中调整。

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
| `max_results` | `3` | 最多展示条数，允许范围 1-10 |
| `min_similarity` | `80` | 最低相似度，允许范围 0-100 |
| `show_match_page_url` | `true` | 是否显示“匹配页”地址 |
| `show_detail_page_url` | `true` | 是否显示“详情页”地址 |
| `show_preview_image` | `true` | 是否显示结果预览图（如果有） |

如果没有结果达到配置的相似度阈值，机器人会明确提示未找到满足条件的结果。

## 网络行为

插件先由 AstrBot 所在电脑直接访问 `https://soutubot.moe/` 建立独立的临时 HTTP 会话，再向 `https://soutubot.moe/api/search` 提交 multipart 表单。它不读取浏览器 Cookie、登录状态或其他浏览器资料，当前接口也未使用账号、API Key 或固定身份凭据。因此换电脑安装后通常可以直接使用，但仍取决于新电脑的网络能否访问网站，以及网站是否调整接口或启用额外的 Cloudflare 验证。遇到 HTTP 403 时插件只会刷新一次普通会话，不包含验证码、浏览器自动化或其他绕过逻辑。

## 开发测试

```powershell
python -m unittest discover -s tests -v
```
