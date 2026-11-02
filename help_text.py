"""Concise feature help using the plugin's active trigger nickname."""


def build_help(nickname: str) -> str:
    nickname = nickname.strip()

    def trigger(command):
        return f"/{command}" + (f" 或 {nickname}{command}" if nickname else "")

    return (
        "📖 搜图帮助\n"
        "图片类功能均支持附图或引用图片后发送指令。\n\n"
        "🖼️ 搜图｜二次元 · 动漫 · 插画\n"
        f"指令：{trigger('搜图')}\n"
        "通过多引擎查询图片来源。\n\n"
        "🐔 找鸡｜福利姬 · Cosplay · 真人照片\n"
        f"指令：{trigger('找鸡')}\n"
        "查找作者与出处，返回一条最匹配的结果；识别可能有误，请自行核对。\n\n"
        "🔎 找本｜以图搜本子\n"
        f"指令：{trigger('找本')}\n"
        "识别作品，自动寻找 JM 或 NH 来源，提供下载。\n\n"
        "📚 搜本｜以名称搜本子\n"
        "指令：/搜本 作品名称\n"
        "也支持引用作品名称后发送 /搜本。\n"
        "按名称查找 JM 作品，匹配后提供下载。\n\n"
        "📥 编号下载\n"
        "指令：JM编号 或 NH编号\n"
        "例如：JM123456、NH123456\n"
        "也支持 /jm 编号、/nh 编号或NH作品链接。\n\n"
        "🧹 清理缓存：/jm清理（仅缓存管理员）\n"
        "❓ 查看帮助：/搜图帮助"
    )
