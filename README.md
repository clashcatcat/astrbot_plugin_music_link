# astrbot_plugin_music_link

适用于 AstrBot 的点歌插件，支持网易云和 QQ 音乐搜索、候选菜单、手动选歌、图片歌单、音频文件发送、歌词和音乐卡片。

## 当前功能

- 支持 `/music search <关键词>` 搜歌
- 支持 `/music pick <关键词> <序号>` 直接选歌
- 支持 `/music id <歌曲ID>` 按 ID 获取歌曲详情
- 支持 `/网易点歌 <关键词>` 和 `/落月点歌 <关键词>` 快速搜索
- 支持 `session_waiter` 交互式等待用户输入序号
- 支持文本歌单和本地 PNG 图片歌单
- 支持可视化配置详情返回字段
- 支持封面、文本、语音、文件四种返回类型
- 支持歌词预览
- 支持主内容合并转发
- 支持 OneBot 音乐卡片
- 支持 AI 工具触发点歌菜单，并自动识别 QQ / 网易音源

## 安装

把本目录放到 AstrBot 的 `data/plugins/astrbot_plugin_music_link` 下，然后让 AstrBot 安装 `requirements.txt` 依赖并加载插件。

## 指令

```text
/music search 蔚蓝档案
/music pick 蔚蓝档案 1
/music id 2608813264
/网易点歌 蔚蓝档案
/落月点歌 蔚蓝档案
```

## AI 点歌

插件提供一个 LLM Tool：

- `play_song_menu(song_name)`

它使用 AstrBot 最新的 `FunctionTool + add_llm_tools()` 方式注册。

适合处理这类请求：

- `来一首七里香`
- `来一首 qq音乐的七里香`
- `网易的晴天`

当前行为：

- 自动识别 `QQ音乐` / `QQ` / `腾讯` / `网易` / `网易云`
- 自动切换到对应音源搜索
- 只发送候选菜单
- 不自动代替用户选歌
- 用户继续发送序号后再返回歌曲详情

## 配置建议

- 推荐 `backend = command9`
- `render_mode = image` 时，插件会用 Pillow 本地生成 PNG 歌单
- `search_list_length` 控制每次展示的候选歌曲数量
- 如果中文字体显示不理想，可以配置 `font_path`
- 如果你希望音乐卡片自动补发，打开 `enable_music_card`

## 返回字段配置

详情返回使用 WebUI 的 `output_fields` 可视化配置，不需要手写 JSON。

每一项可以配置：

- `enable`: 是否启用
- `data`: 字段名
- `describe`: 显示标签
- `type`: 发送类型，支持 `text` / `image` / `audio` / `file`

常见用法：

- 直接发语音：把一项设成 `data = url`、`type = audio`
- 直接发文件：把一项设成 `data = url`、`type = file`
- 附带歌词：启用 `data = lrc`
- 只保留简洁结果：只启用 `name`、`artist`、`url`
- 文字和歌词先发、媒体后发：保持 `separate_media_fields = true`
- 媒体发送前先保留链接文字：保持 `keep_url_text_for_media = true`

## 平台说明

- 音乐卡片当前主要面向 OneBot
- 合并转发建议在 OneBot / Satori 环境测试
- 图片歌单不依赖远端 `t2i`，完全本地生成
