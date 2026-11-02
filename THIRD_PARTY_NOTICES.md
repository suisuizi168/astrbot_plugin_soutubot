# 第三方来源与许可

## 多引擎搜索客户端

以下文件改编自 [Shawlei/astrbot_plugin_soutu_search](https://github.com/Shawlei/astrbot_plugin_soutu_search)，参考提交为 `1f4f863144aafcffbc99aa4a9d89ba2b69c4aec9`：

- `engines/formatter.py`
- `engines/saucenao_client.py`
- `engines/yandex_client.py`
- `engines/ascii2d_client.py`

上游采用 MIT License，原版权声明与完整许可保留在 [engines/LICENSE](engines/LICENSE)。本项目对导入、会话和调用方式进行了适配。

## LLM 入口与 NH 下载流程参考

LLM 工具入口和 NH 下载流程参考了 [sharmanOwO/astrbot_search_download](https://github.com/sharmanOwO/astrbot_search_download)，参考提交为 `1bf9e4aa292ea7981b0b22cd437112eb044f1da2`。

相关功能为独立实现，本仓库及发布包未分发该项目的源文件。流程参考不代表获得了该项目源代码的再分发许可。

## 运行依赖

JM 下载使用 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)，PDF 处理使用 img2pdf、pikepdf 与 Pillow。完整依赖及版本约束见 [requirements.txt](requirements.txt)。

运行依赖由安装环境单独安装，各依赖遵循自身许可；插件源码 ZIP 不打包这些依赖的二进制文件。本项目自身的许可见 [LICENSE](LICENSE)。
