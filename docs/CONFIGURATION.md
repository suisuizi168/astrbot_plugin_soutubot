# 配置手册

[返回 README](../README.md) · [模块与验证](DEVELOPMENT.md)

适用版本：**4.6.1**。插件共 83 项功能配置，默认值以仓库中的 `_conf_schema.json` 为准；新默认值不会覆盖已保存的配置。请在 AstrBot 插件配置页修改。

`general.trigger_nickname`（通用设置 → 触发昵称）默认 `""`，留空不启用昵称指令。填写如 `小岁` 后，附图或引用图片发送 `小岁找鸡`、`小岁找本`、`小岁搜图` 即可调用对应功能；允许昵称后加空格，仅完整匹配当前消息，保留原有权限、开关和冷却。它只服务本插件，不修改 AstrBot 的全局唤醒词，不调用 LLM 解析指令。

## 开关关系

- `llm_tools_enabled` 同时控制四个 LLM 工具入口与 JM 模型辅助；`jm_llm_enabled` 还需总开关开启才生效。任一关闭时，JM 使用原始标题，不做模型关键词提取或候选核验。
- `jm_enabled` 统一控制 JM 查询与下载，包括搜图后续、`/搜本`、明确编号指令及 JM 工具；没有独立的查询总开关。
- `nh_download_enabled` 控制 NH 下载及搜图后的 NH 优先分流，默认开启；这是下载开关，不是独立的 NH 关键词搜索功能。
- `multi_search_enabled` 同时控制 `/搜图` 直接查询和 `/找本` 的多引擎兜底；后者仅在 soutubot 未达标或请求失败时运行。各搜索源还受自身开关控制。
- 进入下载后始终生成并发送 PDF；PDF 配置只控制密码、缓存、清理和大小限制，不提供生成开关。

## 全部配置与默认值

布尔值以“开启/关闭”表示；`[]` 表示空列表，`""` 表示空字符串。数字单位见参数说明。

### LLM

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `llm_tools_enabled` | 开启 | 总开关（工具入口与 JM 辅助）。关闭后不允许 LLM 工具调用，也不使用模型提取 JM 查询词或核验候选；固定指令仍可使用，JM 查询直接传入原始标题。 |
| `jm_llm_enabled` | 开启 | 辅助 JM 查询。不开启的话精度极低。仅在 LLM 总开关开启时生效；关闭时使用原始标题，不做模型关键词提取和候选核验。 |
| `jm_llm_timeout_seconds` | `20` | JM 查询词 LLM 超时（秒）。LLM 超时只跳过 JM 补充，不影响已发送的搜图结果。 范围 3–60。 |
| `jm_max_queries` | `3` | LLM 标题检索词数量。模型最多提取的中日文基础标题词数量；简体中文会额外生成繁体变体并一同查询。 范围 1–5。 |
| `jm_max_authors` | `2` | JM 作者兜底查询数量。标题阶段未确凿命中时，最多并发查询的模型提取作者/社团数量。 范围 1–3。 |
| `jm_llm_verify_threshold` | `85.0` | LLM 候选自动确认阈值。模型只能从 JM 已返回候选中选择；达到此匹配度才自动确认。 范围 70–100。 |

### 搜图与通用

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `whitelist` | `[]` | 会话白名单。留空时全部会话可用；群聊填写群号，私聊填写用户 ID。配置后仅允许列表中的会话使用。 |
| `download_confirmation_timeout_seconds` | `600` | 下载确认有效期（秒）。找本、搜本匹配成功后，原查询者在原会话回复“下载”才开始下载；有效期从询问发送完成起算。范围 30–3600，与下载任务超时独立。 |
| `cooldown_seconds` | `30` | 单用户冷却（秒）。设为 0 可关闭冷却。 |
| `max_pending_tasks` | `20` | 最大待处理任务数。超过上限的新请求会立即失败，不会无限排队。 |
| `strict_mode` | 关闭 | 指令默认使用严格模式。关闭时 factor=1.2，开启时 factor=1.4。 |
| `timeout_seconds` | `300` | 搜图服务网络超时（秒）。底层搜图服务的连接/读取超时，范围 10-600 秒；整轮结果返回时限由“搜图结果返回超时”控制。 |
| `search_result_timeout_seconds` | `60` | 搜图结果返回超时（秒）。从开始搜图到原始结果消息发送完成的总时限；超时会中断本次操作并提示，不再继续 JM 查询。 范围 5–600。 |
| `max_concurrency` | `2` | 最大并发搜索数。建议保持 1-2，避免触发网站限流。 |
| `top_k` | `25` | 网站检索候选数。允许范围 1-50。 |
| `max_results` | `3` | 最多展示条数。允许范围 1-10；超过数量时优先展示相似度更高的结果。 范围 1–10。 |
| `min_similarity` | `80.0` | 最低相似度（%）。允许范围 0-100；只展示相似度达到此数值的结果。 范围 0–100。 |
| `show_match_page_url` | 开启 | 显示匹配页地址。关闭后不在单条结果中显示“匹配页”地址。 |
| `show_detail_page_url` | 开启 | 显示详情页地址。关闭后不在单条结果中显示“详情页”地址。 |
| `show_preview_image` | 开启 | 显示预览图片。结果含有预览图时一并发送；关闭后仅发送文字。 |

### 多引擎

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `multi_search_enabled` | 开启 | 启用多引擎搜图。`/搜图` 直接并行查询已开启的来源，仅展示结果；`/找本` 仅在 soutubot 没有达标结果或请求失败时兜底。关闭后 `/搜图` 提示不可用，`/找本` 仍可查询 soutubot。 |
| `saucenao_enabled` | 开启 | 启用 SauceNAO。多引擎总开关开启时生效；未配置 API Key 会跳过。 |
| `saucenao_api_key` | `""` | SauceNAO API Key。仅用于 SauceNAO 检索，不会发送到聊天或日志。 |
| `saucenao_min_similarity` | `50` | SauceNAO 最低相似度。仅过滤 SauceNAO 结果，与 soutubot 达标阈值独立。 |
| `yandex_enabled` | 开启 | 启用 Yandex。用于一般图片溯源；相关/相似页面不能视为已确认作品。 |
| `ascii2d_enabled` | 关闭 | 启用 ascii2d。依赖网站可访问性；默认关闭，可单独开启。 |
| `ascii2d_bovw` | 关闭 | ascii2d 特征搜索。启用后额外查询特征检索页。 |
| `multi_search_timeout_seconds` | `30` | 每个搜索源总超时（秒）。范围 5–120；一个来源失败不会丢失其他来源结果。 |
| `multi_search_cache_ttl` | `3600` | 多引擎缓存时长（秒）。0 关闭缓存；最多缓存 128 个来源/图片结果。 |

### 找鸡

该组与找本、多引擎搜图独立。进度提示固定为“收到，开始找鸡”，没有关闭提示的开关。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `findchicken_enabled` | True | 找鸡｜启用找鸡；先回复“收到，开始找鸡”，并行搜索后识别作者、账号和出处；沿用白名单，不受多引擎、通用 LLM 工具、JM/NH 开关控制。 |
| `findchicken_api_key` |  | 找鸡｜SauceNAO API Key；仅用于找鸡，与多引擎搜图的 API Key 分开保存；在 SauceNAO 账号的 Search API 页面获取。 |
| `findchicken_proxy` |  | 找鸡｜HTTP/HTTPS 代理；仅用于找鸡，例如 http://127.0.0.1:7890；留空直连，不继承搜图或 JM 的代理配置。 |
| `findchicken_timeout_seconds` | 30 | 找鸡｜查询超时（秒）；分别限制排队及图片准备阶段、SauceNAO 查询阶段；Google 使用独立总超时，一个来源超时不丢失另一来源结果。 |
| `findchicken_candidate_limit` | `30` | 1～60。每个来源供 LLM 筛选的候选上限；Google 每模式最多收集 60 条，SauceNAO 单次最多请求 30 条。最终只展示一条，不改变搜图展示数量。 |
| `findchicken_similarity_threshold` | 30.0 | 找鸡｜最低相似度（%）；只过滤找鸡 SauceNAO 的结果，与搜图设置独立；Google 没有百分比相似度，不使用此阈值。 |
| `findchicken_show_similarity` | False | 找鸡｜显示相似度；开启后在“视觉匹配”后显示接口提供的真实分数；Google 没有百分比分数，不补造。 |
| `findchicken_cooldown_seconds` | 10 | 找鸡｜单用户冷却（秒）；独立于找本、搜图、搜本的冷却记录，0 表示不限制。 |
| `findchicken_llm_enabled` | true | 仅影响找鸡。让 LLM 从所有候选中优先查找原作者并整理名字；没有选中或模型失败时按真实相似度优先、Google 排名其次返回一条匹配来源，不编造缺失信息。不发送原图。关闭后直接展示最高匹配来源。 |
| `findchicken_llm_provider` |  | 找鸡｜作者识别模型；从 Bot 已配置的聊天模型中选择；留空跟随当前会话模型。只需文本能力，用搜索元数据和公开发布页信息整理原作者身份，不重复判断原图。 |
| `findchicken_llm_timeout_seconds` | 60 | 找鸡｜LLM 判断超时（秒）；10～180 秒，独立于搜索超时；每次找鸡最多调用模型一次，不调用工具、不带聊天记录。 |

### JM

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `jm_enabled` | 开启 | 总开关（查询与下载）。统一控制搜图后的 JM 查询与下载、/搜本、/jm 和 JM 工具。关闭后全部 JM 查询、下载停止；NH 和搜图不受影响。 |
| `jm_title_search_enabled` | 开启 | 启用 JM 标题检索。关闭后跳过标题检索，直接尝试作者作品匹配。 |
| `jm_author_search_enabled` | 开启 | 启用 JM 作者作品匹配。仅在标题未确认作品时，按模型提取的作者/社团查询作品并核验。 |
| `jm_retry_times` | `0` | JM 查询重试次数。默认不重试，避免放大等待时间。 范围 0–1。 |
| `jm_max_concurrency` | `5` | JM 查询并发数。标题和作者查询共享此并发上限。 范围 1–5。 |
| `jm_domain_list` | `[]` | JM API 域名优先列表。通常留空以使用 jmcomic 当前域名发现机制。 |
| `jm_proxy` | `"system"` | JM 代理设置。网络排障项；system 使用系统代理，空值不额外指定。不要填写凭据。 |
| `jm_auto_select_threshold` | `90.0` | JM 自动确认阈值。达到阈值且领先候选足够多时才直接显示 JM 号。 范围 80–100。 |
| `jm_min_candidate_threshold` | `55.0` | JM 候选最低阈值。低于此分数不显示候选。 范围 0–90。 |
| `jm_auto_select_margin` | `8.0` | JM 自动确认领先分差。第一名与第二名的最小分差，避免相近标题被误报。 范围 0–30。 |
| `jm_author_max_pages` | `3` | 每位作者最大查询页数。作者作品过多时的网络与响应时间上限。 范围 1–5。 |
| `jm_author_max_candidates` | `60` | 作者兜底候选上限。只将此前若干 JM 官方标题交给模型核验，避免超大响应。 范围 10–100。 |
| `jm_download_timeout_seconds` | `600` | JM 下载超时（秒）。单本从开始下载到 PDF 生成完成的最长时间。 范围 60–3600。 |
| `jm_download_max_concurrency` | `1` | 同时下载的 JM 作品数。限制同时下载的不同作品数量；每本作品内部还会并发下载图片。 范围 1–4。 |
| `jm_download_image_concurrency` | `10` | 单本图片下载并发数。控制同一本作品同时请求的图片数量；数值越大可能越快，也会增加网络压力。 范围 1–30。 |
| `jm_download_photo_concurrency` | `2` | 单本章节下载并发数。控制同一本作品同时处理的章节数量，建议保持默认值。 范围 1–8。 |
| `jm_manage_users` | `[]` | JM 缓存管理员用户 ID。填写允许执行 /jm清理 的用户 ID。留空时关闭手动缓存清理命令；仍须通过插件白名单。 |

### NH

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `nh_download_enabled` | 开启 | 启用 NH 下载。默认开启；soutubot 达标目标包含 NH 编号/链接时优先下载 NH，也允许明确编号指令与 NH 工具调用。已有配置若为关闭则保留原值。 |
| `nh_download_timeout_seconds` | `600` | NH 下载及 PDF 总超时（秒）。范围 60–3600；包含排队、下载和 PDF 生成。 |
| `nh_download_max_concurrency` | `1` | NH 最大同时处理作品数。范围 1–4，与 JM 并发独立。 |
| `nh_image_concurrency` | `4` | NH 图片并发数。范围 1–10；每部作品最多 2000 页、原始图片总大小最多 1 GiB。 |

### PDF · JM

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `jm_pdf_password_enabled` | 开启 | 是否启用 PDF 密码。默认开启；关闭时 PDF 无密码。开启时使用固定密码，固定密码留空则使用纯数字 JM 号。 |
| `jm_pdf_fixed_password` | `""` | PDF 固定密码。仅在启用 PDF 密码时生效；留空使用纯数字 JM 号。实际密码会随上传提示发送到当前会话。 |
| `jm_pdf_cache_enabled` | 开启 | 保留并复用 PDF 缓存。开启后相同 JM 号可直接发送本地 PDF，避免重复访问和下载。 |
| `jm_pdf_cache_ttl_hours` | `168` | PDF 缓存有效期（小时）。默认保留 7 天；设为 0 表示不过期。过期文件会在再次请求该作品时重建。 范围 0–8760。 |
| `jm_max_pdf_size_mb` | `200` | PDF 直接发送大小上限（MB）。超过此大小时不尝试发送，PDF 仍保留在本地缓存；平台自身也可能有更低限制。 范围 1–500。 |
| `jm_cleanup_images_after_pdf` | 开启 | PDF 成功后清理图片缓存。仅在 PDF 已生成并验证有效后删除该作品的原始图片；失败时保留图片以便重试。 |
| `jm_cleanup_pdf_after_send` | 关闭 | 发送成功后删除 PDF。开启后仅在机器人确认发送成功后删除 PDF；默认保留以便后续复用。 |

### PDF · NH

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `nh_pdf_password_enabled` | 开启 | NH PDF 密码。开启时使用固定密码；固定密码留空则使用纯数字 NH 编号。 |
| `nh_pdf_fixed_password` | `""` | NH PDF 固定密码。留空使用 NH 编号；实际密码会随发送提示显示。 |
| `nh_pdf_cache_enabled` | 开启 | NH PDF 缓存。缓存保留 7 天，按密码区分；失败时保留完整图片以便重试 PDF。 |
| `nh_max_pdf_size_mb` | `200` | NH PDF 发送大小上限（MB）。范围 1–500；超出后保留文件。 |

## 固定限制与缓存行为

- JM 子任务、标题查询、作者查询的超时均固定为 **20 秒**，不再开放配置；LLM 超时和下载总超时仍可配置。
- NH 单次网络请求超时为 **30 秒**，最多 **2000 页**，单张原始图片最多 **30 MiB**，原始图片合计最多 **1 GiB**。
- NH PDF 缓存固定保留 **7 天**，生成 PDF 后清理原始图片，发送成功后保留 PDF；关闭缓存后不复用已有 PDF。JM 对应行为可通过上表设置。
- PDF 缓存到期后在再次请求该作品时重建，不是定时删除；PDF 生成失败后保留的完整图片清单可供重试使用。
- 同来源、同编号的进行中任务会合并；JM 和 NH 的任务、并发与缓存独立。
- `/找本`、`/搜本` 的下载确认有效期由 `download_confirmation_timeout_seconds` 控制，默认 **600 秒**；仅原查询者在同一会话回复“下载”有效。

## 升级注意事项

旧的 `search_jm_enabled`、`jm_download_enabled` 已合并到 `jm_enabled`；旧的 `jm_timeout_seconds`、`jm_query_timeout_seconds`、`jm_author_query_timeout_seconds` 已移除。不要继续依赖这些旧字段控制行为。

PDF 字段名保持不变，只调整配置页顺序与说明。4.4.4 起使用原生嵌套分区；旧平铺值自动迁移，升级前保存 `.before-grouping.bak` 备份，隐藏旧字段只作兼容占位。已有 NH 关闭值、PDF 密码与缓存设置会保留。

与 3.x 相比，图片搜索达到相似度阈值后会自动按开关进入下载；升级前请核对 JM/NH 总开关。独立 `/搜本` 保留回复确认流程。

### Google Lens（集中配置）

搜图和找鸡同时查询 Google，不作为找鸡的失败兜底。下列设置独立于其他搜索源，两个入口共用 Key、结果缓存和 Google 请求。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `google_lens_search_enabled` | `true` | 与已开启的多引擎并行；遵循多引擎总开关。不会改变找本的 soutubot 优先流程。 |
| `google_lens_findchicken_enabled` | `true` | 找鸡同时查询 SauceNAO 与 Google Lens，不是失败后兜底；不受多引擎总开关影响。 |
| `google_lens_api_key` | `""` | 在 https://serpapi.com/manage-api-key 获取；两个入口共用此 Key。未填写时跳过 Google，不影响其他来源。 |
| `google_lens_proxy` | `""` | 仅用于 SerpAPI；留空直连，不继承其他搜索源的代理。 |
| `google_lens_timeout_seconds` | `30` | 包含排队、压缩、上传和查询；超时保留其他来源结果。 |
| `google_lens_search_type` | `"all"` | 默认全部，与 Google 网页“全部”入口对应，合并同图、视觉和网页候选；exact_matches 仅同图。Google 不提供百分比相似度。 |
| `google_lens_retry_empty` | `true` | 全部/外观匹配返回空候选时切换另一模式补查一次，最多两次计费搜索，共用原总超时和已上传图片；同图模式不扩展。可关闭以节省额度。 |
| `google_lens_expand_sources` | `true` | 已有结果但没有可解析账号的 X 发布链接时，补查另一模式并合并候选；LLM 优先检查 X，无合适 X 再看其他平台。未知站点仍保留。与空结果补查合计最多两次搜索、共用总超时；可关闭以节省额度。 |
| `google_lens_max_results` | `3` | 1～10，仅控制搜图及找本兜底展示；Google 候选缓存保留每模式最多 60 条，找鸡使用独立候选上限。 |
| `google_lens_cache_ttl` | `3600` | 0 关闭；最多 128 项。非空结果按设置缓存，空结果最多缓存 30 秒，到期绕过上游旧缓存；请求失败不缓存。同图跨入口共享查询。 |

找鸡的 `findchicken_candidate_limit` 控制每个来源的候选数，最终固定一条；相似度阈值仅过滤 SauceNAO，Google 没有百分比分数。找鸡图片准备和 SauceNAO 查询分别应用找鸡超时，Google 应用自己的端到端超时；任一来源失败不丢弃已完成的其他来源。

### 4.4.4 原生配置分区

顶层为 general（通用）、book_search（找本）、image_search（搜图，含共享 Google Lens）、findchicken（找鸡）、jm、nh、pdf。JM 下有 search、llm、download 子区域；PDF 下有 jm、nh，设置各自独立。表内字段名继续保留，所在路径改变；例如作者模型位于 `findchicken.findchicken_llm_provider`，Google 密钥位于 `image_search.google_lens_api_key`，JM PDF 密码位于 `pdf.jm.jm_pdf_fixed_password`。升级会迁移原平铺值、清空隐藏兼容占位并标记迁移完成；随后仅以分区里的值为准。
