from __future__ import annotations

import asyncio
import ast
from dataclasses import dataclass
from typing import Any
import math
import json
import mimetypes
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.star.star_tools import StarTools
try:
    from botpy.types.inline import Keyboard, Button, RenderData, Action, Permission, KeyboardRow
    from botpy.types.message import KeyboardPayload, MarkdownPayload
except ImportError:
    Keyboard = Button = RenderData = Action = Permission = KeyboardRow = KeyboardPayload = MarkdownPayload = None
try:
    from botpy.http import Route
except ImportError:
    Route = None
from astrbot.core.utils.session_waiter import SessionController, SessionFilter, session_waiter


# 版本号的唯一来源：@register 和歌单图片页脚都用它，改这里就够了。
# metadata.yaml 里的 version 是给 AstrBot 插件市场看的，记得一起改。
PLUGIN_VERSION = "1.3.0"

PLATFORM_LABELS = {
    "netease": "网易云",
    "tencent": "QQ音乐",
    "kugou": "酷狗音乐",
}

PLATFORM_TAGS = {
    "netease": ("网易", "#e4393c"),
    "tencent": ("QQ", "#31c27c"),
    "kugou": ("酷狗", "#2f86ff"),
}

COMMAND6_API_URLS = {
    "api.injahow.cn": "https://api.injahow.cn/meting/?type=url&id={id}",
    "meting.jmstrand.cn": "https://meting.jmstrand.cn/?type=url&id={id}",
    "api.qijieya.cn": "https://api.qijieya.cn/meting/?type=url&id={id}",
    "metingapi.nanorocky.top": "https://metingapi.nanorocky.top/?server=netease&type=url&id={id}",
}

DEFAULT_LUOYUE_API_URL = "https://api.vkeys.cn"


class MusicSelectionSessionFilter(SessionFilter):
    """Keep a group's song-selection session scoped to its requesting user."""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"music-link-selection:{event.unified_msg_origin}:{event.get_sender_id()}"


def to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def split_exit_words(raw: Any) -> list[str]:
    if not raw:
        return ["0", "不听了", "退出"]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return ""
    minutes, remain = divmod(seconds, 60)
    return f"{minutes:02d}:{remain:02d}"


def parse_cn_duration(text: Any) -> int:
    if not text:
        return 0
    match = re.search(r"(\d+)分(\d+)秒", str(text))
    if not match:
        return 0
    return int(match.group(1)) * 60 + int(match.group(2))


def normalize_command9_platforms(value: Any) -> list[str]:
    valid_platforms = {"netease", "tencent", "kugou"}
    if value == "aggregation":
        return ["netease", "tencent"]
    raw_platforms = value if isinstance(value, list) else [value]
    platforms: list[str] = []
    for platform in raw_platforms:
        normalized = str(platform or "").strip().lower()
        if normalized in valid_platforms and normalized not in platforms:
            platforms.append(normalized)
    return platforms or ["netease"]


def first_config_url(config: AstrBotConfig, list_key: str, legacy_key: str, default: str) -> str:
    values = config.get(list_key)
    if isinstance(values, list):
        for value in values:
            url = str(value or "").strip()
            if url:
                return url
    if isinstance(values, str) and values.strip():
        return values.strip()

    legacy_value = str(config.get(legacy_key, "") or "").strip()
    return legacy_value or default


@dataclass
class SongItem:
    song_id: str
    source_backend: str
    platform: str
    platform_label: str
    name: str
    artist: str
    album: str
    duration_seconds: int
    duration_text: str
    cover: str
    quality: str
    raw: dict[str, Any]


@dataclass
class SongDetail:
    song_id: str
    platform: str
    name: str
    artist: str
    album: str
    duration_seconds: int
    quality: str
    cover: str
    url: str
    lyric: str
    size: str
    kbps: str
    raw: dict[str, Any]

    def as_field_map(self) -> dict[str, str]:
        platform_label = PLATFORM_LABELS.get(self.platform, self.platform or "未知平台")
        return {
            "id": self.song_id,
            "name": self.name,
            "artist": self.artist,
            "album": self.album,
            "platform": platform_label,
            "quality": self.quality,
            "pic": self.cover,
            "url": self.url,
            "lrc": self.lyric,
            "size": self.size,
            "kbps": self.kbps,
            "duration": format_duration(self.duration_seconds),
        }


@pydantic_dataclass
class PlaySongMenuTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None
    name: str = "play_song_menu"
    description: str = (
        "当用户想点歌、听歌时调用。支持根据用户提到的来源关键词自动选择 QQ 音乐或网易云，"
        "只发送候选菜单，不自动代替用户选歌。工具已经直接给用户发消息，调用后不要额外重复回复。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "歌曲名称或包含歌手的搜索关键词，例如：qq音乐的七里香、网易的晴天。",
                },
            },
            "required": ["song_name"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        plugin = self.plugin
        event = context.context.event
        song_name = str(kwargs.get("song_name", "")).strip()
        if not plugin or not song_name:
            return "未提供歌曲关键词。"

        keyword, force_platform = plugin._parse_ai_song_request(song_name)
        if not keyword:
            return "未能提取有效的歌曲关键词。"

        asyncio.create_task(
            plugin._send_song_menu_and_wait(
                event,
                keyword,
                force_platform,
            )
        )
        return ""


@pydantic_dataclass
class SearchSongCandidatesTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None
    name: str = "search_song_candidates"
    description: str = (
        "当你需要替用户自动挑歌时，先调用这个工具搜索候选歌曲。它不会给用户发消息，只会把候选列表返回给你，"
        "你看完结果后，再调用 play_song_direct 发送你认为最合适的那一首。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "歌曲名称或包含来源的关键词，例如：来首七里香、qq音乐的搁浅、网易的晴天。",
                },
            },
            "required": ["song_name"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        plugin = self.plugin
        song_name = str(kwargs.get("song_name", "")).strip()
        if not plugin or not song_name:
            return "未提供歌曲关键词。"

        keyword, force_platform = plugin._parse_ai_song_request(song_name)
        if not keyword:
            return "未能提取有效的歌曲关键词。"
        return await plugin._search_song_candidates_for_llm(keyword, force_platform)


@pydantic_dataclass
class PlaySongDirectTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None
    name: str = "play_song_direct"
    description: str = (
        "在你已经通过 search_song_candidates 看过候选列表后调用。根据你选定的候选序号，"
        "直接把对应歌曲发送给用户，不展示候选菜单。工具已经直接给用户发消息，调用后不要额外重复回复。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "和 search_song_candidates 相同的搜索关键词。",
                },
                "candidate_index": {
                    "type": "integer",
                    "description": "你从候选列表里选中的序号，从 1 开始。",
                },
            },
            "required": ["song_name", "candidate_index"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        plugin = self.plugin
        event = context.context.event
        song_name = str(kwargs.get("song_name", "")).strip()
        candidate_index = to_int(kwargs.get("candidate_index"), 0)
        if not plugin or not song_name:
            return "未提供歌曲关键词。"
        if candidate_index < 1:
            return "未提供有效的候选序号。"

        keyword, force_platform = plugin._parse_ai_song_request(song_name)
        if not keyword:
            return "未能提取有效的歌曲关键词。"

        asyncio.create_task(
            plugin._send_song_by_index(
                event,
                keyword,
                force_platform,
                candidate_index,
            )
        )
        return ""


def truncate_text(text: Any, max_length: int) -> str:
    value = str(text or "")
    if len(value) <= max_length:
        return value
    return value[: max_length - 2] + ".."


INDEX_BADGES = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# QQ 官方机器人的两个 AstrBot 适配器：网关(WS)模式与 webhook 模式。
# 两者的发送 API、markdown、keyboard 完全一致，按钮逻辑对二者都适用。
QQ_OFFICIAL_PLATFORMS = ("qq_official", "qq_official_webhook")
# QQ 官方键盘的硬性限制：最多 5 行，每行最多 5 个按钮。
QQ_KEYBOARD_MAX_ROWS = 5
# 连续这么多次发出回调按钮都收不到点击事件，才判定该部署不支持并降级为指令按钮。
# 取 2 是为了容忍「用户只是没在时限内点」这种误判。
CALLBACK_DOWNGRADE_STREAK = 2
# botpy Intents 的互动事件位（botpy/flags.py: interaction = 1 << 26，public_messages = 1 << 25）。
# AstrBot 的 qq_official 适配器没有申报 interaction，不申报则 QQ 网关不会推送回调按钮的
# INTERACTION_CREATE 事件，客户端点击时表现为「请求第三方失败 / 请求超时」。
# 参照 Zhalslar/astrbot_plugin_music 的做法，补位时连同 public_messages 一起补，
# 只补 1<<26 会把原有的消息位覆盖掉/漏掉，导致仍然收不到事件。
QQ_INTENT_INTERACTION_BIT = 1 << 26
QQ_INTENT_PUBLIC_MESSAGES_BIT = 1 << 25
QQ_INTENT_CALLBACK_BITS = QQ_INTENT_PUBLIC_MESSAGES_BIT | QQ_INTENT_INTERACTION_BIT
# 按钮样式：0 灰色线框 / 1 蓝色线框 / 4 蓝底白字（实心）。官方 wiki 只列了 0、1，
# 4 是实测可用的实心蓝底样式，观感最贴近“药丸按钮”。
QQ_BUTTON_STYLE_PRIMARY = 4
# 一行一个按钮时每页放几首：QQ 键盘上限 5 行，留最后一行给「下一页 / 退出」。
QQ_KEYBOARD_PAGE_SIZE = 4
# 一行一个按钮时标签可以放完整「序号. 歌名 - 歌手」，这里只做超长兜底，实际由客户端截断。
QQ_BUTTON_LABEL_WIDTH = 30
# 指令按钮模式下「下一页」点击后实际发出的文字，等待逻辑靠它翻页。
NEXT_PAGE_WORDS = ("下一页", "下页", "翻页")
# 指令按钮模式下「上一页」点击后实际发出的文字。
PREV_PAGE_WORDS = ("上一页", "上页")
# 旧版客户端不支持该操作时的提示文案。
QQ_BUTTON_UNSUPPORT_TIPS = "当前版本不支持该操作，请更新 QQ"


def index_badge(index: int) -> str:
    if 1 <= index <= len(INDEX_BADGES):
        return INDEX_BADGES[index - 1]
    return str(index)


def display_width(text: Any) -> int:
    """按手机端观感估算宽度：中日韩全角字符按 2 计，其余按 1 计。"""
    total = 0
    for char in str(text or ""):
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 1
    return total


def clip_by_width(text: Any, max_width: int) -> str:
    """按显示宽度裁剪文本，超长时以省略号收尾，避免按钮文字换行/挤压。"""
    value = str(text or "").strip()
    if display_width(value) <= max_width:
        return value
    budget = max(1, max_width - display_width("…"))
    used = 0
    chars = []
    for char in value:
        width = 2 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 1
        if used + width > budget:
            break
        chars.append(char)
        used += width
    return "".join(chars).rstrip() + "…"


def quality_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    quality_map = {
        1: "标准",
        2: "标准",
        3: "HQ",
        4: "HQ",
        5: "SQ",
        6: "Hi-Res",
        7: "杜比",
        8: "沉浸",
        9: "母带",
    }
    if isinstance(value, int):
        return quality_map.get(value, str(value))
    return str(value)


DEFAULT_OUTPUT_FIELDS = [
    {"data": "pic", "describe": "封面", "type": "image"},
    {"data": "name", "describe": "歌曲", "type": "text"},
    {"data": "artist", "describe": "歌手", "type": "text"},
    {"data": "platform", "describe": "平台", "type": "text"},
    {"data": "album", "describe": "专辑", "type": "text"},
    {"data": "quality", "describe": "音质", "type": "text"},
    {"data": "duration", "describe": "时长", "type": "text"},
    {"data": "url", "describe": "下载链接", "type": "text"},
]


def parse_output_fields(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return DEFAULT_OUTPUT_FIELDS
    if isinstance(raw, list):
        valid_items = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized.pop("__template_key", None)
            valid_items.append(normalized)
        return valid_items or DEFAULT_OUTPUT_FIELDS
    return DEFAULT_OUTPUT_FIELDS


def sanitize_filename(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name).strip()
    return safe or "music-file"


def guess_extension(url: str, content_type: str) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    suffix = Path(url.split("?")[0]).suffix
    return suffix or ".bin"


class LocalSongListRenderer:
    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name

    def render(self, songs: list[SongItem], keyword: str, config: AstrBotConfig) -> str:
        output_dir = StarTools.get_data_dir(self.plugin_name) / "rendered_songlists"
        output_dir.mkdir(parents=True, exist_ok=True)
        png_path = output_dir / f"songlist_{uuid4().hex}.png"
        self._draw_png(songs, keyword, config, png_path)
        return str(png_path)

    def _draw_png(self, songs: list[SongItem], keyword: str, config: AstrBotConfig, png_path: Path) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"加载 Pillow 失败: {exc}") from exc

        dark_mode = to_bool(config.get("svg_dark_mode"), False)
        theme_color = str(config.get("svg_theme_color", "#4f7cff"))
        width = max(420, to_int(config.get("svg_width"), 760))
        columns = max(1, min(4, to_int(config.get("svg_columns"), 2)))
        layout_mode = str(config.get("svg_column_layout_mode", "row-first"))
        show_dividers = to_bool(config.get("svg_show_dividers"), True)
        show_background = to_bool(config.get("svg_show_song_background"), True)
        show_version = to_bool(config.get("svg_show_version_info"), True)
        scale = max(1.0, float(config.get("svg_scale", 1.8)))

        canvas_width = int(width * scale)
        padding = int(18 * scale)
        header_height = int(62 * scale)
        item_height = int(58 * scale)
        footer_height = int((56 if show_version else 26) * scale)
        items_per_column = max(1, math.ceil(len(songs) / columns))
        rows = max(1, math.ceil(len(songs) / columns))
        total_height = header_height + rows * item_height + footer_height + padding * 2
        height = max(int(180 * scale), total_height)
        col_width = (canvas_width - padding * 2) / columns
        card_width = col_width - int(8 * scale)

        bg_color = "#0d1117" if dark_mode else "#f4f7fb"
        card_bg = "#161b22" if dark_mode else "#ffffff"
        text_color = "#e6edf3" if dark_mode else "#1f2937"
        sub_text_color = "#8b949e" if dark_mode else "#6b7280"
        divider_color = "#30363d" if dark_mode else "#d7dfeb"
        watermark_color = "#6e7681" if dark_mode else "#93a1b6"

        image = Image.new("RGB", (canvas_width, height), bg_color)
        draw = ImageDraw.Draw(image)

        title_font = self._load_font(config.get("font_path"), int(17 * scale), ImageFont)
        subtitle_font = self._load_font(config.get("font_path"), int(12 * scale), ImageFont)
        song_font = self._load_font(config.get("font_path"), int(14 * scale), ImageFont)
        meta_font = self._load_font(config.get("font_path"), int(11 * scale), ImageFont)
        tag_font = self._load_font(config.get("font_path"), int(9 * scale), ImageFont)
        footer_font = self._load_font(config.get("font_path"), int(9 * scale), ImageFont)
        index_font = self._load_font(config.get("font_path"), int(12 * scale), ImageFont)

        draw.rounded_rectangle(
            (padding, padding, canvas_width - padding, height - padding),
            radius=int(14 * scale),
            fill=card_bg,
            outline=theme_color,
            width=max(1, int(scale)),
        )
        draw.rounded_rectangle(
            (padding, padding, canvas_width - padding, padding + header_height - int(12 * scale)),
            radius=int(14 * scale),
            fill=self._blend_with_white(theme_color, 0.90 if not dark_mode else 0.82),
        )

        self._draw_text(draw, (padding + int(16 * scale), padding + int(10 * scale)), "music-link 候选歌单", title_font, theme_color)
        self._draw_text(
            draw,
            (padding + int(16 * scale), padding + int(33 * scale)),
            f"关键词: {truncate_text(keyword, 36)}",
            subtitle_font,
            sub_text_color,
        )
        total_text = f"共 {len(songs)} 首"
        total_w, _ = self._measure_text(draw, total_text, subtitle_font)
        self._draw_text(
            draw,
            (canvas_width - padding - int(16 * scale) - total_w, padding + int(10 * scale)),
            total_text,
            subtitle_font,
            sub_text_color,
        )
        draw.line(
            (
                padding + int(14 * scale),
                padding + int(52 * scale),
                canvas_width - padding - int(14 * scale),
                padding + int(52 * scale),
            ),
            fill=self._blend_with_white(theme_color, 0.65 if not dark_mode else 0.45),
            width=max(1, int(scale)),
        )

        for index, song in enumerate(songs):
            if layout_mode == "column-first":
                col = index // items_per_column
                row = index % items_per_column
            else:
                row = index // columns
                col = index % columns
            x = int(padding + col * col_width)
            y = int(padding + header_height + row * item_height)

            if show_background:
                fill = "#1f2937" if dark_mode else "#f8fbff"
                draw.rounded_rectangle(
                    (x, y, x + int(card_width), y + item_height - int(6 * scale)),
                    radius=int(10 * scale),
                    fill=fill,
                    outline=self._blend_with_white(theme_color, 0.82 if not dark_mode else 0.55),
                    width=1,
                )

            index_box = (x + int(10 * scale), y + int(14 * scale), x + int(34 * scale), y + int(38 * scale))
            draw.rounded_rectangle(index_box, radius=int(7 * scale), fill=self._blend_with_white(theme_color, 0.82 if not dark_mode else 0.45))
            index_text = str(index + 1)
            index_w, index_h = self._measure_text(draw, index_text, index_font)
            self._draw_text(
                draw,
                (
                    index_box[0] + (index_box[2] - index_box[0] - index_w) / 2,
                    index_box[1] + (index_box[3] - index_box[1] - index_h) / 2,
                ),
                index_text,
                index_font,
                theme_color,
            )

            text_x = x + int(42 * scale)
            self._draw_text(draw, (text_x, y + int(8 * scale)), truncate_text(song.name, 18), song_font, text_color)
            self._draw_text(draw, (text_x, y + int(26 * scale)), truncate_text(song.artist, 22), meta_font, sub_text_color)
            if song.album:
                self._draw_text(draw, (text_x, y + int(40 * scale)), truncate_text(song.album, 24), tag_font, sub_text_color)

            tags: list[tuple[str, str, str]] = []
            if song.platform:
                platform_text, platform_color = PLATFORM_TAGS.get(song.platform, (song.platform, theme_color))
                tags.append((platform_text, "#ffffff", platform_color))
            qtext = quality_text(song.quality)
            if qtext:
                tags.append((qtext, theme_color, "#eef4ff" if not dark_mode else "#21262d"))
            if song.duration_text:
                tags.append((song.duration_text, sub_text_color, "#eef2f7" if not dark_mode else "#21262d"))

            tag_right = x + int(card_width) - int(10 * scale)
            for tag_index, (tag_text, tag_color, tag_fill) in enumerate(tags[:3]):
                text_w, text_h = self._measure_text(draw, tag_text, tag_font)
                tag_width = max(int(34 * scale), min(int(72 * scale), text_w + int(14 * scale)))
                tag_y = y + int(6 * scale) + tag_index * int(17 * scale)
                draw.rounded_rectangle(
                    (tag_right - tag_width, tag_y, tag_right, tag_y + int(14 * scale)),
                    radius=int(4 * scale),
                    fill=tag_fill,
                )
                self._draw_text(
                    draw,
                    (
                        tag_right - tag_width / 2 - text_w / 2,
                        tag_y + (int(14 * scale) - text_h) / 2,
                    ),
                    tag_text,
                    tag_font,
                    tag_color,
                )

            if show_dividers:
                draw.line(
                    (
                        x + int(8 * scale),
                        y + item_height - int(7 * scale),
                        x + int(card_width) - int(8 * scale),
                        y + item_height - int(7 * scale),
                    ),
                    fill=divider_color,
                    width=1,
                )

        if show_version:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            footer_lines = [
                "generated by astrbot plugin: astrbot_plugin_music_link",
                f"version: {PLUGIN_VERSION}",
                f"date_time: {timestamp}",
            ]
            line_height = int(11 * scale)
            start_y = height - padding - line_height * len(footer_lines) - int(10 * scale)
            for line in footer_lines:
                draw.text((padding + int(8 * scale), start_y), line, font=footer_font, fill=watermark_color)
                start_y += line_height

        image.save(png_path, format="PNG")

    @staticmethod
    def _load_font(font_path: Any, size: int, image_font_module):
        candidates = []
        if font_path:
            candidates.append(str(font_path))
        candidates.extend(
            [
                # macOS
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/STHeiti Light.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
                "/Library/Fonts/Arial Unicode.ttf",
                # Windows：以前这里只有 macOS 的路径，Windows 上一律回落到
                # load_default()，那是个点阵字体，中文全画成方块/空白。
                "C:/Windows/Fonts/msyh.ttc",
                "C:/Windows/Fonts/msyhl.ttc",
                "C:/Windows/Fonts/simhei.ttf",
                "C:/Windows/Fonts/deng.ttf",
                "C:/Windows/Fonts/simsun.ttc",
                # Linux
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
        )
        for candidate in candidates:
            try:
                if candidate and Path(candidate).exists():
                    return image_font_module.truetype(candidate, size=size)
            except Exception:
                continue
        # 最后兜底也把字号带上：不带 size 的 load_default() 固定 11px，
        # 在放大过的画布上小得看不见。老版本 Pillow 不支持 size 参数，再退一层。
        try:
            return image_font_module.load_default(size=size)
        except Exception:
            return image_font_module.load_default()

    @staticmethod
    def _blend_with_white(hex_color: str, ratio: float) -> tuple[int, int, int]:
        color = hex_color.lstrip("#")
        if len(color) != 6:
            return (240, 244, 255)
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)
        ratio = max(0.0, min(1.0, ratio))
        return (
            int(r * (1 - ratio) + 255 * ratio),
            int(g * (1 - ratio) + 255 * ratio),
            int(b * (1 - ratio) + 255 * ratio),
        )

    @staticmethod
    def _measure_text(draw, text: str, font) -> tuple[int, int]:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    @staticmethod
    def _draw_text(draw, position: tuple[float, float], text: str, font, fill) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        x, y = position
        draw.text((x, y - bbox[1]), text, font=font, fill=fill)


class MusicService:
    def __init__(self, config: AstrBotConfig):
        self.config = config
        self.timeout = to_int(config.get("request_timeout_seconds"), 20)

    async def _get_json(self, url: str) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                return response.json()
            text = response.text.strip()
            return response.json() if text.startswith("{") or text.startswith("[") else text

    @staticmethod
    def _preview_payload(payload: Any, max_length: int = 120) -> str:
        text = str(payload or "").replace("\n", " ").replace("\r", " ").strip()
        if len(text) <= max_length:
            return text
        return text[: max_length - 3] + "..."

    def _require_mapping(self, payload: Any, context: str) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        preview = self._preview_payload(payload)
        raise ValueError(f"{context}返回了非 JSON 对象: {preview or '空内容'}")

    def _optional_mapping(self, payload: Any) -> dict[str, Any]:
        return payload if isinstance(payload, dict) else {}

    def _luoyue_api_base_url(self) -> str:
        return first_config_url(
            self.config,
            "luoyue_api_urls",
            "luoyue_api_base_url",
            DEFAULT_LUOYUE_API_URL,
        ).rstrip("/")

    async def search_command6(self, keyword: str, limit: int) -> list[SongItem]:
        api = (
            "http://music.163.com/api/search/get/web"
            f"?csrf_token=hlpretag=&hlposttag=&s={quote(keyword)}"
            f"&type=1&offset=0&total=true&limit={limit}"
        )
        data = self._require_mapping(await self._get_json(api), "网易云搜索接口")
        result = self._optional_mapping(data.get("result"))
        songs = result.get("songs") or []
        if not isinstance(songs, list):
            raise ValueError(f"网易云搜索接口返回了异常 songs 字段: {self._preview_payload(songs)}")
        items: list[SongItem] = []
        for song in songs:
            if not isinstance(song, dict):
                logger.warning(f"网易云搜索结果里包含异常歌曲项: {self._preview_payload(song)}")
                continue
            artists = "/".join(artist.get("name", "") for artist in song.get("artists", []))
            duration_seconds = to_int(song.get("duration"), 0) // 1000
            album_data = self._optional_mapping(song.get("album"))
            album = album_data.get("name", "")
            items.append(
                SongItem(
                    song_id=str(song.get("id", "")),
                    source_backend="command6",
                    platform="netease",
                    platform_label="网易云",
                    name=song.get("name", "未知歌曲"),
                    artist=artists or "未知歌手",
                    album=album,
                    duration_seconds=duration_seconds,
                    duration_text=format_duration(duration_seconds),
                    cover=album_data.get("picUrl", ""),
                    quality="",
                    raw=song,
                )
            )
        return items

    async def get_command6_detail(self, song_id: str, api_url: str) -> SongDetail:
        detail_api = f"http://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
        lyric_api = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"

        detail_data = self._require_mapping(await self._get_json(detail_api), "网易云详情接口")
        songs = detail_data.get("songs") or []
        if not isinstance(songs, list):
            raise ValueError(f"网易云详情接口返回了异常 songs 字段: {self._preview_payload(songs)}")
        if not songs:
            raise ValueError("网易云歌曲详情为空")
        song = songs[0]
        if not isinstance(song, dict):
            raise ValueError(f"网易云详情接口返回了异常歌曲项: {self._preview_payload(song)}")

        lyric_text = ""
        try:
            lyric_data = self._require_mapping(await self._get_json(lyric_api), "网易云歌词接口")
            lyric_text = (self._optional_mapping(lyric_data.get("lrc")).get("lyric", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取网易云歌词失败: {exc}")

        song_url = self._build_command6_song_url(api_url, song_id)

        artists = "/".join(artist.get("name", "") for artist in song.get("artists", []))
        duration_seconds = to_int(song.get("duration"), 0) // 1000
        album_data = self._optional_mapping(song.get("album"))
        return SongDetail(
            song_id=str(song.get("id", "")),
            platform="netease",
            name=song.get("name", "未知歌曲"),
            artist=artists or "未知歌手",
            album=album_data.get("name", ""),
            duration_seconds=duration_seconds,
            quality="",
            cover=album_data.get("picUrl", ""),
            url=song_url,
            lyric=lyric_text,
            size="",
            kbps="",
            raw=song,
        )

    @staticmethod
    def _build_command6_song_url(api_url: str, song_id: str) -> str:
        endpoint = COMMAND6_API_URLS.get(api_url, api_url).strip()
        if not endpoint:
            raise ValueError("未配置网易 API 地址")
        if "{id}" in endpoint:
            return endpoint.replace("{id}", quote(song_id))
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}type=url&id={quote(song_id)}"

    async def search_command9(
        self,
        keyword: str,
        limit: int,
        platforms: list[str],
        netease_quality: int,
        qq_quality: int,
        kugou_quality: str,
    ) -> list[SongItem]:
        base_url = self._luoyue_api_base_url()
        selected_platforms = normalize_command9_platforms(platforms)
        if len(selected_platforms) > 1:
            per_platform_limit = max(1, math.ceil(limit / len(selected_platforms)))
            urls = [
                (
                    platform,
                    f"{base_url}/v2/music/{platform}?word={quote(keyword)}"
                    f"&num={per_platform_limit}&quality="
                    f"{self._command9_quality(platform, netease_quality, qq_quality, kugou_quality)}",
                )
                for platform in selected_platforms
            ]
            responses = await self._gather_json(*(url for _, url in urls))
            # Some Luoyue-compatible endpoints ignore `num`; enforce the
            # per-platform allocation locally so the rendered list stays bounded.
            groups = [
                self._normalize_command9_items(response, platform, PLATFORM_LABELS[platform])[
                    :per_platform_limit
                ]
                for (platform, _), response in zip(urls, responses, strict=True)
            ]
            merged: list[SongItem] = []
            for index in range(max((len(group) for group in groups), default=0)):
                for group in groups:
                    if index < len(group):
                        merged.append(group[index])
            return merged

        platform = selected_platforms[0]
        quality = self._command9_quality(platform, netease_quality, qq_quality, kugou_quality)
        url = (
            f"{base_url}/v2/music/{platform}?word={quote(keyword)}"
            f"&num={limit}&quality={quality}"
        )
        data = await self._get_json(url)
        platform_label = PLATFORM_LABELS.get(platform, platform)
        return self._normalize_command9_items(data, platform, platform_label)[:limit]

    @staticmethod
    def _command9_quality(
        platform: str,
        netease_quality: int,
        qq_quality: int,
        kugou_quality: str,
    ) -> int | str:
        if platform == "netease":
            return netease_quality
        if platform == "kugou":
            return kugou_quality
        return qq_quality

    async def _gather_json(self, *urls: str) -> tuple[Any, ...]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            responses = await asyncio.gather(*[client.get(url) for url in urls], return_exceptions=True)
            results: list[Any] = []
            for response in responses:
                if isinstance(response, Exception):
                    logger.warning(f"落月聚合搜索请求失败: {response}")
                    results.append({"code": 500, "data": []})
                    continue
                try:
                    response.raise_for_status()
                    results.append(response.json())
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"落月聚合搜索响应异常: {exc}")
                    results.append({"code": 500, "data": []})
            return tuple(results)

    def _normalize_command9_items(self, payload: Any, platform: str, platform_label: str) -> list[SongItem]:
        if not isinstance(payload, dict):
            logger.warning(f"落月搜索接口返回了非 JSON 对象: {self._preview_payload(payload)}")
            return []
        if not payload or payload.get("code") != 200 or not payload.get("data"):
            return []
        raw_items = payload["data"] if isinstance(payload["data"], list) else [payload["data"]]
        items: list[SongItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                logger.warning(f"落月搜索结果里包含异常歌曲项: {self._preview_payload(raw)}")
                continue
            duration_seconds = parse_cn_duration(raw.get("interval"))
            song_id = raw.get("id") or (raw.get("hash") if platform == "kugou" else "")
            items.append(
                SongItem(
                    song_id=str(song_id or ""),
                    source_backend="command9",
                    platform=platform,
                    platform_label=platform_label,
                    name=raw.get("song") or raw.get("name") or raw.get("SongName") or raw.get("title") or "未知歌曲",
                    artist=raw.get("singer") or raw.get("artist") or raw.get("SingerName") or "未知歌手",
                    album=raw.get("album") or "",
                    duration_seconds=duration_seconds,
                    duration_text=format_duration(duration_seconds),
                    cover=raw.get("cover") or raw.get("pic") or "",
                    quality=raw.get("quality") or "",
                    raw=raw,
                )
            )
        return items

    async def get_command9_detail(
        self,
        song_id: str,
        platform: str,
        quality: int | str,
        song_raw: dict[str, Any] | None = None,
        keyword: str = "",
    ) -> SongDetail:
        base_url = self._luoyue_api_base_url()
        request_params = self._build_command9_detail_params(platform, song_id, song_raw)
        detail_api = f"{base_url}/v2/music/{platform}?{request_params}&quality={quote(str(quality))}"

        detail_data = self._require_mapping(await self._get_json(detail_api), "落月详情接口")
        if not detail_data or detail_data.get("code") != 200 or not detail_data.get("data"):
            raise ValueError("落月 API 返回空详情")
        raw = self._require_mapping(detail_data["data"], "落月详情数据")

        lyric_text = ""
        try:
            lyric_params = self._build_command9_lyric_params(platform, song_id, song_raw, raw, keyword)
            lyric_api = f"{base_url}/v2/music/{platform}/lyric?{lyric_params}"
            lyric_data = self._require_mapping(await self._get_json(lyric_api), "落月歌词接口")
            lyric_text = (self._optional_mapping(lyric_data.get("data")).get("lrc", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取落月 API 歌词失败: {exc}")

        duration_seconds = parse_cn_duration(raw.get("interval"))
        return SongDetail(
            song_id=str(raw.get("id") or raw.get("hash") or song_id),
            platform=platform,
            name=raw.get("song") or raw.get("name") or raw.get("SongName") or (song_raw.get("name") if song_raw else None) or (song_raw.get("song") if song_raw else None) or "未知歌曲",
            artist=raw.get("singer") or raw.get("artist") or raw.get("SingerName") or (song_raw.get("singer") if song_raw else None) or (song_raw.get("artist") if song_raw else None) or "未知歌手",
            album=raw.get("album") or "",
            duration_seconds=duration_seconds,
            quality=raw.get("quality") or "",
            cover=raw.get("cover") or raw.get("pic") or "",
            url=raw.get("url") or "",
            lyric=lyric_text,
            size=raw.get("size") or "",
            kbps=raw.get("kbps") or "",
            raw=raw,
        )

    def _build_command9_detail_params(
        self,
        platform: str,
        song_id: str,
        song_raw: dict[str, Any] | None,
    ) -> str:
        if platform != "kugou":
            return f"id={quote(song_id)}"

        raw = song_raw or {}
        song_hash = str(raw.get("hash") or song_id or "").strip()
        if not song_hash:
            raise ValueError("酷狗歌曲缺少 hash，无法获取详情")
        params = [f"hash={quote(song_hash)}"]
        for key in ("album_id", "album_audio_id"):
            camel_key = "albumID" if key == "album_id" else "albumAudioID"
            value = raw.get(key) or raw.get(camel_key)
            if value:
                params.append(f"{key}={quote(str(value))}")
        return "&".join(params)

    def _build_command9_lyric_params(
        self,
        platform: str,
        song_id: str,
        song_raw: dict[str, Any] | None,
        detail_raw: dict[str, Any],
        keyword: str,
    ) -> str:
        if platform != "kugou":
            return f"id={quote(song_id)}"

        raw = {**(song_raw or {}), **detail_raw}
        lyric_id = raw.get("lyric_id")
        accesskey = raw.get("accesskey")
        if lyric_id and accesskey:
            return f"id={quote(str(lyric_id))}&accesskey={quote(str(accesskey))}"

        song_hash = raw.get("hash") or song_id
        if not song_hash:
            raise ValueError("酷狗歌曲缺少歌词查询参数")
        params = [f"hash={quote(str(song_hash))}"]
        album_audio_id = raw.get("album_audio_id") or raw.get("albumAudioID")
        if album_audio_id:
            params.append(f"album_audio_id={quote(str(album_audio_id))}")
        lyric_keyword = keyword or raw.get("song") or raw.get("name")
        if lyric_keyword:
            params.append(f"keyword={quote(str(lyric_keyword))}")
        if raw.get("fmt"):
            params.append(f"fmt={quote(str(raw['fmt']))}")
        return "&".join(params)


@register("astrbot_plugin_music_link", "VincentZyuApps / Codex", "AstrBot 点歌插件", PLUGIN_VERSION)
class MusicLinkPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = MusicService(config)
        self.song_list_renderer = LocalSongListRenderer("astrbot_plugin_music_link")
        # 回调按钮点击后 QQ 推送 interaction 事件，用会话键找回当前待选的歌单。
        self._pending_button_picks: dict[str, dict[str, Any]] = {}
        # 当前选歌卡片的消息 ID（按会话键），选完歌后撤回它。
        self._selection_card_ids: dict[str, str] = {}
        # 同一条 msg_id 被动回复多次时要递增 msg_seq（翻页会用到）。
        self._msg_seq_counters: dict[str, int] = {}
        # 回执 interaction 用的 botpy api，接管 parser 拿到原始 dict 时对象上没有 _api。
        self._last_qq_api: Any = None
        # 是否曾经收到过任何按钮回调事件，用于在超时时给出准确的排障提示。
        self._interaction_ever_received = False
        # 回调按钮被证明收不到事件时置位，之后自动改用指令按钮（本次运行内生效）。
        self._callback_buttons_unavailable = False
        # 连续多少次「发了回调按钮却等到超时、期间零事件」才降级为指令按钮。
        self._callback_timeout_streak = 0
        self.context.add_llm_tools(
            PlaySongMenuTool(plugin=self),
            SearchSongCandidatesTool(plugin=self),
            PlaySongDirectTool(plugin=self),
        )

    async def initialize(self):
        logger.info(
            f"astrbot_plugin_music_link 已初始化 (v{PLUGIN_VERSION}, "
            f"按钮={'回调' if self._use_callback_buttons() else '指令'}模式, "
            f"按钮开关={to_bool(self.config.get('enable_qq_keyboard'), True)})"
        )
        if self._use_callback_buttons():
            self._ensure_interaction_intent()

    def _ensure_interaction_intent(self) -> None:
        """给 qq_official 适配器补上 botpy 的 interaction intent。

        intent 是连接时 IDENTIFY 一次性上报的，AstrBot 的适配器没有申报 interaction（bit 26），
        网关就不会推送回调按钮的 INTERACTION_CREATE，客户端表现为「请求第三方失败」。
        补位时连同 public_messages（bit 25）一起补 —— 参照 Zhalslar/astrbot_plugin_music，
        只补 interaction 一位在实测中收不到事件。
        """
        try:
            adapters = list(
                self.context.platform_manager.get_insts()
                if hasattr(self.context.platform_manager, "get_insts")
                else self.context.platform_manager.platform_insts or []
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music_link] 拿不到平台适配器列表，无法补 interaction intent: {exc}")
            return

        for adapter in adapters:
            intents = getattr(adapter, "intents", None)
            # 适配器优先用 get_client()（AstrBot 官方入口），拿不到再退回 .client 属性。
            client = None
            getter = getattr(adapter, "get_client", None)
            if callable(getter):
                try:
                    client = getter()
                except Exception:  # noqa: BLE001
                    client = None
            if client is None:
                client = getattr(adapter, "client", None)
            if intents is None and client is None:
                continue
            # botpy 的 Client 把 intents 存成 int（Client.__init__: self.intents = intents.value）。
            client_intents = getattr(client, "intents", None)
            if isinstance(client_intents, int) and client_intents & QQ_INTENT_INTERACTION_BIT:
                logger.info("[music_link] qq_official 已申报 interaction intent，无需补")
                self._log_interaction_readiness(adapter, client)
                return

            patched = False
            if intents is not None and hasattr(intents, "interaction"):
                try:
                    intents.interaction = True
                    patched = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[music_link] 设置 intents.interaction 失败: {exc}")
            if isinstance(client_intents, int):
                try:
                    client.intents = client_intents | QQ_INTENT_CALLBACK_BITS
                    patched = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[music_link] 设置 client.intents 失败: {exc}")
            # botpy 的 IDENTIFY 读的是 session dict 里的 "intent"（gateway.ws_identify），
            # 已登录的连接只能靠改这里让下次重连带上这些位。
            session_patched = 0
            try:
                sessions = getattr(getattr(client, "_connection", None), "session_pool", None)
                if sessions is None:
                    sessions = getattr(getattr(client, "_connection", None), "_session_pool", None)
                for session in list(sessions or []):
                    if isinstance(session, dict) and isinstance(session.get("intent"), int):
                        session["intent"] |= QQ_INTENT_CALLBACK_BITS
                        session_patched += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[music_link] 补 session intent 失败（忽略）: {exc}")

            if patched:
                logger.warning(
                    "[music_link] 已给 qq_official 补上 interaction+public_messages intent"
                    f"（回调按钮所需，已修正 {session_patched} 个连接会话）。botpy 的 IDENTIFY 只在"
                    "建立连接时上报 intents —— 若本次是在适配器登录之后才打的补丁，需要重启 "
                    "AstrBot 才会生效；重启后若点击按钮仍提示「请求第三方失败」，插件会自动降级为指令按钮。"
                )
                self._log_interaction_readiness(adapter, client)
                return

        logger.warning(
            "[music_link] 未找到可补 interaction intent 的 qq_official 适配器"
            f"（共 {len(adapters)} 个平台实例）。回调按钮可能收不到点击事件。"
        )

    def _log_interaction_readiness(self, adapter: Any, client: Any) -> None:
        """打印一条能定位「事件为什么不来」的诊断。

        关键在于区分两种部署，它们的修法完全不同：
        - WS（网关）模式：intents 在 IDENTIFY 时一次性上报。已登录（session_id 非空）时
          再补 intents 对本次连接无效，必须重启/重连。
        - webhook 模式：intents 无用，收哪些事件由 QQ 开放平台的「事件订阅」决定，
          没勾选互动事件时无论代码怎么改都收不到。
        """
        if getattr(self, "_readiness_logged", False):
            return
        self._readiness_logged = True
        try:
            adapter_cls = type(adapter).__name__
            is_webhook = "webhook" in adapter_cls.lower()
            client_intents = getattr(client, "intents", None)
            bits = "未知"
            if isinstance(client_intents, int):
                bits = (
                    f"{client_intents}"
                    f" (public_messages={bool(client_intents & QQ_INTENT_PUBLIC_MESSAGES_BIT)},"
                    f" interaction={bool(client_intents & QQ_INTENT_INTERACTION_BIT)})"
                )
            sessions = getattr(getattr(client, "_connection", None), "session_pool", None) or []
            session_states = []
            for session in list(sessions):
                if not isinstance(session, dict):
                    continue
                intent = session.get("intent")
                logged_in = bool(session.get("session_id"))
                session_states.append(
                    f"intent={intent}"
                    f"/interaction={bool(isinstance(intent, int) and intent & QQ_INTENT_INTERACTION_BIT)}"
                    f"/已登录={logged_in}"
                )
            logger.warning(
                "[music_link] 按钮回调就绪诊断: "
                f"适配器={adapter_cls}（{'webhook 模式：intents 无效，看开放平台事件订阅' if is_webhook else 'WS 模式：intents 决定推送'}）, "
                f"client.intents={bits}, 连接会话=[{'; '.join(session_states) or '无'}]。"
                "判读：WS 模式下若某会话「已登录=True 且 interaction=False」，说明补丁晚于登录、"
                "本次连接收不到事件，需重启 AstrBot；若 interaction=True 仍收不到，"
                "则是 QQ 侧未给该 bot 推送互动事件，需在 QQ 开放平台勾选「互动事件」。"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[music_link] 就绪诊断打印失败（忽略）: {exc}")

    async def _download_remote_file(self, url: str, filename_hint: str) -> str:
        output_dir = StarTools.get_data_dir("astrbot_plugin_music_link") / "downloads"
        output_dir.mkdir(parents=True, exist_ok=True)
        timeout = to_int(self.config.get("request_timeout_seconds"), 20)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            ext = guess_extension(url, response.headers.get("content-type", ""))
            filename = sanitize_filename(filename_hint)
            if not filename.endswith(ext):
                filename += ext
            file_path = output_dir / filename
            file_path.write_bytes(response.content)
            return str(file_path)

    def _supports_forward_nodes(self, event: AstrMessageEvent) -> bool:
        platform = event.get_platform_name()
        return platform in {"aiocqhttp", "satori"}

    def _supports_music_card(self, event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "aiocqhttp"

    def _supports_markdown(self, event: AstrMessageEvent) -> bool:
        # 只有 QQ 官方系原生渲染 markdown；OneBot(aiocqhttp)/satori/telegram 等按纯文本处理。
        return event.get_platform_name() in QQ_OFFICIAL_PLATFORMS

    def _wrap_primary_with_forward(
        self,
        event: AstrMessageEvent,
        components: list[Comp.BaseMessageComponent],
    ) -> list[Comp.BaseMessageComponent]:
        if not to_bool(self.config.get("use_forward_for_primary"), False):
            return components
        if not self._supports_forward_nodes(event):
            return components
        if not components:
            return components
        node = Comp.Node(
            content=components,
            name=event.get_sender_name() or "music-link",
            uin=event.get_self_id() or event.get_sender_id() or "0",
        )
        return [Comp.Nodes([node])]

    async def _build_detail_components(
        self,
        event: AstrMessageEvent,
        detail: SongDetail,
    ) -> tuple[list[Comp.BaseMessageComponent], list[list[Comp.BaseMessageComponent]]]:
        field_map = detail.as_field_map()
        specs = parse_output_fields(self.config.get("output_fields"))
        components: list[Comp.BaseMessageComponent] = []
        deferred_messages: list[list[Comp.BaseMessageComponent]] = []
        text_lines: list[str] = []
        supports_md = self._supports_markdown(event)
        lyric_limit = to_int(self.config.get("lyric_preview_length"), 600)
        download_files = to_bool(self.config.get("download_files_before_send"), False)
        separate_media = to_bool(self.config.get("separate_media_fields"), True)
        keep_url_text_for_media = to_bool(self.config.get("keep_url_text_for_media"), True)
        explicit_url_text_enabled = any(
            isinstance(spec, dict)
            and spec.get("enable") is not False
            and str(spec.get("data", "")).strip() == "url"
            and str(spec.get("type", "text")).strip() == "text"
            for spec in specs
        )

        for spec in specs:
            if spec.get("enable") is False:
                continue
            field_name = str(spec.get("data", "")).strip()
            field_type = str(spec.get("type", "text")).strip()
            label = str(spec.get("describe", field_name)).strip() or field_name
            value = field_map.get(field_name, "")
            if not value:
                continue

            if field_type == "text":
                text_value = value
                if field_name == "lrc":
                    text_value = value.strip()[:lyric_limit]
                
                # 为 URL 类型的文本字段添加 Markdown 链接，使其在 QQ 等平台可点击
                is_url_text = field_name == "url" or (
                    isinstance(text_value, str) and re.match(r"^https?://", text_value.strip())
                )
                if supports_md and is_url_text:
                    # 将下载链接改为可点击的链接
                    link_label = "点击查看/下载" if field_name == "url" else text_value
                    text_lines.append(f"{label}: [{link_label}]({text_value})")
                else:
                    # OneBot 等不渲染 markdown 的平台：标签 + 纯网址
                    text_lines.append(f"{label}: {text_value}")
                continue

            if text_lines:
                components.append(Comp.Plain("\n".join(text_lines)))
                text_lines = []

            if field_type == "image":
                components.append(Comp.Image.fromURL(value))
            elif field_type == "audio":
                if field_name == "url" and keep_url_text_for_media and not explicit_url_text_enabled:
                    if supports_md:
                        text_lines.append(f"{label}: [点击播放/下载]({value})")
                    else:
                        text_lines.append(f"{label}: {value}")
                audio_message = [Comp.Record.fromURL(value)]
                if separate_media:
                    deferred_messages.append(audio_message)
                else:
                    components.extend(audio_message)
            elif field_type == "file":
                if field_name == "url" and keep_url_text_for_media and not explicit_url_text_enabled:
                    if supports_md:
                        text_lines.append(f"{label}: [点击播放/下载]({value})")
                    else:
                        text_lines.append(f"{label}: {value}")
                filename_hint = f"{detail.name}-{detail.artist}"
                file_component: Comp.File
                if download_files:
                    try:
                        local_path = await self._download_remote_file(value, filename_hint)
                        file_component = Comp.File(name=Path(local_path).name, file=local_path)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"下载文件后发送失败，回退 URL 文件发送: {exc}")
                        file_component = Comp.File(name=sanitize_filename(filename_hint) + ".mp3", url=value)
                else:
                    file_component = Comp.File(name=sanitize_filename(filename_hint) + ".mp3", url=value)

                file_message = [file_component]
                if separate_media:
                    deferred_messages.append(file_message)
                else:
                    components.extend(file_message)

        if text_lines:
            components.append(Comp.Plain("\n".join(text_lines)))
        if not components and deferred_messages:
            components = [Comp.Plain("已发送媒体内容，请查看后续消息。")]
        return components or [Comp.Plain("没有可发送的详情字段。")], deferred_messages

    def _build_music_card_payload(self, detail: SongDetail) -> dict[str, Any] | None:
        if detail.platform == "netease":
            return {
                "type": "music",
                "data": {
                    "type": "163",
                    "id": int(detail.song_id),
                },
            }

        if detail.platform == "tencent":
            raw = detail.raw or {}
            jump_url = raw.get("link") or raw.get("jumpUrl")
            if not jump_url:
                song_mid = raw.get("mid")
                jump_url = (
                    f"https://y.qq.com/n/ryqq/songDetail/{song_mid}"
                    if song_mid
                    else f"https://y.qq.com/n/ryqq/songDetail/{detail.song_id}"
                )
            return {
                "type": "music",
                "data": {
                    "type": "custom",
                    "url": jump_url,
                    "audio": detail.url,
                    "title": detail.name,
                    "content": detail.artist,
                    "image": detail.cover,
                },
            }

        return None

    def _should_send_music_card(
        self,
        event: AstrMessageEvent,
    ) -> bool:
        if not to_bool(self.config.get("enable_music_card"), False):
            return False
        if not self._supports_music_card(event):
            return False
        return True

    async def _send_music_card(self, event: AstrMessageEvent, detail: SongDetail) -> None:
        if not self._should_send_music_card(event):
            return
        payload = self._build_music_card_payload(detail)
        if not payload:
            return

        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None) if bot is not None else None
        if api is None or not hasattr(api, "call_action"):
            logger.warning("当前事件不支持直接发送 OneBot 音乐卡片")
            return

        is_group = False
        session_id = None
        if hasattr(event, "is_private_chat") and callable(getattr(event, "is_private_chat")):
            is_group = not event.is_private_chat()
            session_id = event.get_group_id() if is_group else event.get_sender_id()
        else:
            is_group = bool(event.get_group_id())
            session_id = event.get_group_id() if is_group else event.get_sender_id()

        if session_id is None or str(session_id) == "":
            logger.warning("发送音乐卡片失败：无法获取有效的会话 ID")
            return

        try:
            if is_group:
                payloads = {"message": [payload], "group_id": session_id}
                await api.call_action("send_group_msg", **payloads)
            else:
                payloads = {"message": [payload], "user_id": session_id}
                await api.call_action("send_private_msg", **payloads)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"发送音乐卡片失败，已忽略: {exc}")

    async def _yield_detail_results(self, event: AstrMessageEvent, detail: SongDetail):
        primary_components, deferred_messages = await self._build_detail_components(event, detail)
        yield event.chain_result(self._wrap_primary_with_forward(event, primary_components))
        for message in deferred_messages:
            await self._send_deferred_media(event, message)
        await self._send_music_card(event, detail)

    async def _send_detail_followups(self, event: AstrMessageEvent, detail: SongDetail) -> None:
        primary_components, deferred_messages = await self._build_detail_components(event, detail)
        await event.send(event.chain_result(self._wrap_primary_with_forward(event, primary_components)))
        for message in deferred_messages:
            await self._send_deferred_media(event, message)
        await self._send_music_card(event, detail)

    async def _send_deferred_media(
        self,
        event: AstrMessageEvent,
        message: list[Comp.BaseMessageComponent],
    ) -> None:
        try:
            await event.send(event.chain_result(message))
        except Exception as exc:  # noqa: BLE001
            # QQ's client-side voice service can time out after the song details
            # have already been fetched successfully; do not fail the whole request.
            logger.warning(f"发送音频/文件失败，已忽略: {exc}")

    
    def _supports_qq_keyboard(self, event: AstrMessageEvent) -> bool:
        if Keyboard is None:
            return False
        # AstrBot 有两个 QQ 官方适配器：网关模式 qq_official 和 webhook 模式
        # qq_official_webhook，两者发消息的 API 完全一样，按钮都支持。
        return event.get_platform_name() in QQ_OFFICIAL_PLATFORMS

    def _use_callback_buttons(self) -> bool:
        """回调按钮：点击不往聊天里发消息，QQ 直接推 interaction 事件给机器人。

        若上一轮已证明这个部署收不到 interaction 事件（点击后 QQ 报「请求第三方失败」），
        则自动降级为指令按钮，避免按钮点了没反应。
        """
        if self._callback_buttons_unavailable:
            return False
        return to_bool(self.config.get("qq_keyboard_callback"), True)

    def _build_qq_button(
        self,
        button_id: str,
        label: str,
        visited_label: str,
        data: str,
        callback: bool,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        if callback:
            # type=1 回调按钮：data 原样回传到 interaction 的 button_data 里。
            # permission 用 type=0 + specify_user_ids 限定发起人可点（同 Zhalslar 的实现），
            # 并加 click_limit=1 防连点；拿不到发起人时退回 type=2（所有人可点）。
            if owner_id:
                permission = Permission(type=0, specify_role_ids=[], specify_user_ids=[owner_id])
            else:
                permission = Permission(type=2)
            action = Action(
                type=1,
                permission=permission,
                click_limit=1,
                data=data,
                at_bot_show_channel_list=False,
                unsupport_tips=QQ_BUTTON_UNSUPPORT_TIPS,
            )
        else:
            # type=2 指令按钮：enter=True 表示点击后直接把 data 当成消息发出去。
            action = Action(
                type=2,
                permission=Permission(type=2),
                data=data,
                enter=True,
                reply=False,
                unsupport_tips=QQ_BUTTON_UNSUPPORT_TIPS,
            )
        return Button(
            id=button_id,
            render_data=RenderData(label=label, visited_label=visited_label, style=QQ_BUTTON_STYLE_PRIMARY),
            action=action,
        )

    def _build_qq_keyboard_payload(
        self, songs: list[SongItem], owner_id: str | None = None, page: int = 1
    ) -> dict[str, Any] | None:
        if not songs or Keyboard is None:
            return None

        # 一行一个通栏按钮，标签放完整「序号. 歌名 - 歌手」（太长交给客户端截断）。
        # QQ 键盘上限 5 行，所以每页 4 首 + 最后一行放「下一页 / 退出」。
        callback = self._use_callback_buttons()
        total_pages = max(1, math.ceil(len(songs) / QQ_KEYBOARD_PAGE_SIZE))
        page = max(1, min(page, total_pages))
        start = (page - 1) * QQ_KEYBOARD_PAGE_SIZE
        page_songs = songs[start : start + QQ_KEYBOARD_PAGE_SIZE]

        rows: list[dict[str, Any]] = []
        for offset, song in enumerate(page_songs):
            index = start + offset + 1  # 序号始终是全局的，和上方歌单文字一致
            label = f"{index}. {song.name}"
            if song.artist:
                label = f"{label} - {song.artist}"
            label = clip_by_width(label, QQ_BUTTON_LABEL_WIDTH)
            rows.append(
                KeyboardRow(
                    buttons=[
                        self._build_qq_button(
                            f"pick_{index}", label, f"▶ {label}", str(index), callback, owner_id
                        )
                    ]
                )
            )

        exit_words = split_exit_words(self.config.get("exit_words"))
        exit_word = exit_words[0] if exit_words else "0"
        controls = []
        if page > 1:
            prev_page = page - 1
            controls.append(
                self._build_qq_button(
                    f"page_{prev_page}",
                    "⏮ 上一页",
                    "⏮ 翻页中…",
                    f"page:{prev_page}" if callback else PREV_PAGE_WORDS[0],
                    callback,
                    owner_id,
                )
            )
        if page < total_pages:
            next_page = page + 1
            controls.append(
                self._build_qq_button(
                    f"page_{next_page}",
                    "⏭ 下一页",
                    "⏭ 翻页中…",
                    f"page:{next_page}" if callback else NEXT_PAGE_WORDS[0],
                    callback,
                    owner_id,
                )
            )
        controls.append(
            self._build_qq_button(
                "pick_0", "✖️ 退出点歌", "✖️ 已退出", "0" if callback else exit_word, callback, owner_id
            )
        )
        # 「上一页 / 下一页 / 退出」并排放最后一行：既保住 5 行上限，也让翻页和退出都始终可见。
        rows.append(KeyboardRow(buttons=controls))
        return KeyboardPayload(content=Keyboard(rows=rows))

    async def _get_qq_keyboard_if_supported(
        self, event: AstrMessageEvent, songs: list[SongItem], page: int = 1
    ) -> dict[str, Any] | None:
        platform = event.get_platform_name()
        if not self._supports_qq_keyboard(event):
            logger.info(
                f"[music_link] 不发按钮：平台={platform}（需要 {' 或 '.join(QQ_OFFICIAL_PLATFORMS)}）, "
                f"botpy键盘类型={'可用' if Keyboard is not None else '未安装'}"
            )
            return None
        if not to_bool(self.config.get("enable_qq_keyboard"), True):
            logger.info("[music_link] 不发按钮：enable_qq_keyboard 已关闭")
            return None
        callback = self._use_callback_buttons()
        total_pages = max(1, math.ceil(len(songs) / QQ_KEYBOARD_PAGE_SIZE))
        logger.info(
            f"[music_link] 构建按钮键盘：平台={platform}, 候选={len(songs)} 首, "
            f"第 {page}/{total_pages} 页, 模式={'回调(type=1)' if callback else '指令(type=2)'}"
        )
        owner_id = None
        if callback:
            # 每次发按钮前都补一次 intent：插件初始化可能早于适配器登录，
            # 也可能晚于登录（那次无效），发送时再补一次能覆盖到重连后的连接。
            self._ensure_interaction_intent()
            try:
                owner_id = str(event.get_sender_id() or "") or None
            except Exception:  # noqa: BLE001
                owner_id = None
        return self._build_qq_keyboard_payload(songs, owner_id, page)

    
    async def _send_unified_qq_official_message(
        self, 
        event: AstrMessageEvent, 
        chain: list[Comp.BaseMessageComponent], 
        keyboard: dict[str, Any] = None
    ):
        bot = getattr(event, "bot", None)
        source = getattr(event.message_obj, "raw_message", None)
        if not bot or not source:
            logger.warning(
                f"[music_link] 无法走 QQ 官方通道发送：bot={'有' if bot else '缺失'}, "
                f"raw_message={'有' if source else '缺失'}，按钮不会出现"
            )
            return

        import botpy.message
        plain_text = "".join([comp.text for comp in chain if isinstance(comp, Comp.Plain)])

        # 同一条 msg_id 被动回复多次（翻页就是）必须带上递增的 msg_seq，否则 QQ 判重丢弃。
        msg_id = event.message_obj.message_id
        seq = self._msg_seq_counters.get(msg_id, 0) + 1
        self._msg_seq_counters[msg_id] = seq
        payload = {
            "msg_id": msg_id,
            "msg_type": 2,
            "msg_seq": seq,
            "markdown": {"content": plain_text.strip() or "🎵 请选择歌曲："}
        }
        if keyboard:
            payload["keyboard"] = keyboard
            # 回调按钮的点击不产生消息，只推 interaction 事件，所以发键盘前先挂好监听。
            self._install_interaction_hook(bot)

        try:
            result = None
            if isinstance(source, botpy.message.GroupMessage):
                result = await bot.api.post_group_message(group_openid=source.group_openid, **payload)
            elif isinstance(source, botpy.message.C2CMessage):
                method = getattr(bot.api, "post_c2c_message", None)
                if method:
                    result = await method(openid=source.author.user_openid, **payload)
                else:
                    logger.warning("[music_link] botpy 缺少 post_c2c_message，私聊无法发送按钮")
                    return
            else:
                result = await bot.api.post_message(channel_id=source.channel_id, **payload)
            sent_id = self._extract_sent_message_id(result)
            logger.info(
                f"[music_link] 已发送 markdown{'+keyboard' if keyboard else '（无按钮）'} "
                f"到 {type(source).__name__}"
                + (f"，message_id={sent_id}" if sent_id else "，未取到 message_id（无法撤回）")
            )
            return sent_id
        except Exception as exc:
            logger.warning(
                f"[QQOfficial] 发送失败（原生 markdown 权限未开通时会报这个），"
                f"已回退为纯文本、按钮丢失: {exc}"
            )
            await event.send(event.plain_result(plain_text))
        return None

    @staticmethod
    def _extract_sent_message_id(result: Any) -> str | None:
        """从发送接口的返回里取出消息 ID，撤回卡片要用。"""
        if result is None:
            return None
        value = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        if value in (None, ""):
            # 取不到就没法撤回，把返回体的形状打出来，便于对症适配。
            shape = (
                f"dict keys={sorted(result.keys())}"
                if isinstance(result, dict)
                else f"{type(result).__name__} attrs="
                + str([a for a in dir(result) if not a.startswith('_')][:12])
            )
            logger.warning(f"[music_link] 发送返回里没有消息 ID，无法撤回。返回体: {shape}")
            return None
        return str(value)

    async def _recall_qq_message(self, event: AstrMessageEvent, message_id: str) -> bool:
        """撤回 QQ 官方消息（v2 群/私聊走 DELETE 路由，频道用 recall_message）。"""
        bot = getattr(event, "bot", None)
        source = getattr(event.message_obj, "raw_message", None)
        if bot is None or source is None or not message_id:
            return False

        import botpy.message

        try:
            # 先判具体类型，最后才回落到频道消息：某些 botpy 版本里 DirectMessage/GroupMessage
            # 是 Message 的子类，先判 Message 会把它们错误地送去 channel_id 分支。
            route_path = None
            route_params: dict[str, Any] = {}
            if isinstance(source, botpy.message.GroupMessage):
                route_path = "/v2/groups/{group_openid}/messages/{message_id}"
                route_params = {"group_openid": source.group_openid}
            elif isinstance(source, botpy.message.C2CMessage):
                route_path = "/v2/users/{openid}/messages/{message_id}"
                route_params = {"openid": source.author.user_openid}
            elif isinstance(source, botpy.message.DirectMessage):
                route_path = "/dms/{guild_id}/messages/{message_id}"
                route_params = {"guild_id": source.guild_id}
            elif isinstance(source, botpy.message.Message):
                await bot.api.recall_message(channel_id=source.channel_id, message_id=message_id)
                return True
            else:
                return False

            if Route is None:
                logger.debug("[music_link] botpy 缺少 Route，无法撤回群/私聊消息")
                return False

            await bot.api._http.request(
                Route("DELETE", route_path, message_id=message_id, **route_params)
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music_link] 撤回选歌卡片失败，已忽略: {exc}")
            return False

    @staticmethod
    def _dig(source: Any, *names: str) -> Any:
        """interaction 既可能是 botpy 的对象也可能是原始 dict，两种都兼容地取值。"""
        for name in names:
            value = None
            if isinstance(source, dict):
                value = source.get(name)
            else:
                value = getattr(source, name, None)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _coerce_mapping(value: Any) -> Any:
        """只把「dict 的字符串形式」还原成 dict，其余原样返回。

        webhook 模式下 data/resolved 有时是字符串，形如
        "{'type': 11, 'resolved': {'button_data': 'page:2'}}"：单引号 + Python 的 None，
        json.loads 解不了，得用 ast.literal_eval。
        注意不能把「不是 dict 也不是 str」的值吞成 {} —— botpy 那边它是对象，
        后面还要靠 _dig 的 getattr 取值（吞掉就会全变成 None）。
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text.startswith("{"):
            return value
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            try:
                parsed = ast.literal_eval(text)
            except Exception:  # noqa: BLE001
                return value
        return parsed if isinstance(parsed, dict) else value

    @staticmethod
    def _scrape_button_fields(*sources: Any) -> tuple[str | None, str | None]:
        """从事件的文本形式里正则抠出 button_data / button_id。

        最后一道兜底：webhook 的 data/resolved 可能是 dict、dict 的字符串、也可能是
        botpy 对象，逐层取值任一环断掉就会整块丢失。而这两个字段无论哪种形态，
        在 repr 里都长成 'button_data': 'page:2' 或 "button_data": "page:2"。
        """
        button_data = button_id = None
        for source in sources:
            if source is None:
                continue
            text = source if isinstance(source, str) else repr(source)
            for field in ("button_data", "button_id"):
                if (button_data if field == "button_data" else button_id) is not None:
                    continue
                match = re.search(
                    rf"['\"]?{field}['\"]?\s*[:=]\s*(?:['\"](?P<v>[^'\"]*)['\"]|(?P<n>\d+))",
                    text,
                )
                if not match:
                    continue
                value = match.group("v") if match.group("v") is not None else match.group("n")
                if value in (None, "", "None"):
                    continue
                if field == "button_data":
                    button_data = value
                else:
                    button_id = value
            if button_data is not None and button_id is not None:
                break
        return button_data, button_id

    @staticmethod
    def _clean_openid(value: Any) -> str | None:
        """webhook payload 里空字段是字符串 "None"，不能当成有效会话键。"""
        if value is None:
            return None
        text = str(value).strip()
        return None if text in ("", "None", "none", "null") else text

    def _install_interaction_hook(self, bot: Any) -> None:
        """给 botpy client 挂上按钮回调监听（两层，互为兜底）。

        1) `bot.on_interaction_create`：botpy 的 Client.ws_dispatch 用
           `hasattr(self, "on_" + event)` 反射找处理器（找不到只打一条 debug 日志），
           而 AstrBot 的 qq_official 适配器没定义它，所以挂到实例上即可收到。
        2) `client._connection.state.parsers["interaction_create"]`：直接接管 parser 拿原始
           payload。botpy 的 Interaction 模型在 payload 缺 `resolved` 时会抛 AttributeError，
           那种情况下事件到不了第 1 层，只有这一层能收到。
        """
        if bot is None:
            return
        api = getattr(bot, "api", None)
        if api is not None:
            self._last_qq_api = api
        if getattr(bot, "_music_link_interaction_plugin", None) is self:
            return
        if getattr(bot, "_music_link_interaction_hooked", False):
            # 插件热重载后 bot 上挂的还是旧实例的回调：旧实例的待选登记是空的，
            # 点击会一路走到「找不到待选会话，当前登记键=[]」。所以必须重新绑到当前实例。
            logger.warning(
                "[QQOfficial] 检测到 bot 上挂的是旧插件实例的按钮回调（插件重载过），"
                "已重新绑定到当前实例，否则点击会一直报「找不到待选会话」。"
            )
        plugin = self

        async def dispatch(interaction: Any) -> None:
            try:
                await plugin._handle_qq_interaction(interaction)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[QQOfficial] 处理按钮回调失败: {exc}")

        try:
            bot.on_interaction_create = dispatch
            bot._music_link_interaction_hooked = True
            bot._music_link_interaction_plugin = self
            logger.info("[QQOfficial] 已挂载按钮回调监听 on_interaction_create")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[QQOfficial] 挂载 on_interaction_create 失败: {exc}")

        # 第 2 层：接管 parser。成功后不再回调 botpy 原实现，避免一次点击被处理两次。
        parsers = getattr(getattr(getattr(bot, "_connection", None), "state", None), "parsers", None)
        if not isinstance(parsers, dict):
            logger.warning(
                "[QQOfficial] 拿不到 botpy parsers，按钮回调只依赖 on_interaction_create；"
                "若点击后报请求超时，请关闭 qq_keyboard_callback 回退为指令按钮"
            )
            return

        def parse_interaction_create(payload: Any) -> None:
            raw = payload.get("d", payload) if isinstance(payload, dict) else payload
            try:
                asyncio.get_running_loop().create_task(dispatch(raw))
            except RuntimeError:  # 不在事件循环里（理论上不会发生）
                logger.warning("[QQOfficial] 按钮回调无法调度：当前不在事件循环中")

        try:
            parsers["interaction_create"] = parse_interaction_create
            logger.info("[QQOfficial] 已接管 botpy interaction_create parser")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[QQOfficial] 接管 interaction_create parser 失败: {exc}")

    async def _ack_interaction(self, interaction: Any, code: int = 0) -> None:
        """回执 interaction。QQ 等不到回执就会在客户端弹「请求超时」，所以必须最先做。"""
        interaction_id = self._dig(interaction, "id", "interaction_id")
        api = (
            getattr(interaction, "_api", None)
            or getattr(interaction, "api", None)
            or self._last_qq_api
        )
        method = getattr(api, "on_interaction_result", None) if api is not None else None
        if not interaction_id:
            logger.warning("[QQOfficial] 按钮回执失败：interaction 里没有 id")
            return
        if method is None:
            logger.warning(
                "[QQOfficial] 按钮回执失败：拿不到 botpy api.on_interaction_result，"
                f"interaction 类型={type(interaction).__name__}"
            )
            return
        try:
            await method(interaction_id, code)
            logger.info(f"[QQOfficial] 按钮回执成功 id={interaction_id} code={code}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[QQOfficial] 按钮回执请求失败 id={interaction_id} code={code}: {exc}")

    async def _handle_qq_interaction(self, interaction: Any) -> None:
        self._interaction_ever_received = True
        logger.info(f"[QQOfficial] 收到按钮回调事件: {interaction!r}")
        data = self._coerce_mapping(self._dig(interaction, "data"))
        resolved = self._coerce_mapping(self._dig(data, "resolved"))
        button_data = self._dig(resolved, "button_data")
        button_id = self._dig(resolved, "button_id")
        if button_data in (None, "") and button_id in (None, ""):
            # 兜底：不管 data/resolved 是 dict、字符串还是 botpy 对象，
            # 直接从整个事件的文本形式里把两个字段抠出来，绝不因为层级取不到就丢掉点击。
            button_data, button_id = self._scrape_button_fields(interaction, data, resolved)
            logger.info(
                f"[QQOfficial] 逐层取不到按钮字段，已从事件文本兜底解析: "
                f"button_data={button_data!r} button_id={button_id!r} "
                f"(data 类型={type(data).__name__}, resolved 类型={type(resolved).__name__})"
            )
        # 优先用 button_data，缺失时从 id（形如 pick_3 / page_2）里兜底解析。
        raw_value = button_data
        if raw_value in (None, "") and isinstance(button_id, str):
            if button_id.startswith("pick_"):
                raw_value = button_id[len("pick_") :]
            elif button_id.startswith("page_"):
                raw_value = f"page:{button_id[len('page_') :]}"

        # 先回执再干活：QQ 的等待窗口很短，取歌详情要走网络，绝不能挡在回执前面。
        await self._ack_interaction(interaction, 0)

        if raw_value in (None, ""):
            logger.warning(
                f"[QQOfficial] 按钮回调里没有数据，忽略。button_data={button_data!r} button_id={button_id!r}"
            )
            return

        # 翻页按钮的 data 形如 "page:2"，其余是歌曲序号。
        text_value = str(raw_value)
        if text_value.startswith("page:"):
            kind, value = "page", to_int(text_value[len("page:") :], 0)
            if value < 1:
                logger.warning(f"[QQOfficial] 翻页按钮页码无效: {text_value!r}")
                return
        else:
            kind, value = "pick", to_int(text_value, 0)

        candidate_keys = self._interaction_session_keys(interaction)
        pending = None
        for key in candidate_keys:
            pending = self._pending_button_picks.get(key)
            if pending is not None:
                break
        if pending is None:
            logger.warning(
                "[QQOfficial] 按钮回调找不到待选会话（可能已超时/已选过）。"
                f"回调键={candidate_keys} 当前登记键={list(self._pending_button_picks)}"
            )
            return

        # 翻页不需要等待协程参与：直接用登记时存下的 event/songs 重发该页卡片。
        # 之前靠队列转交给 _wait_for_song_selection 处理，那个协程一结束翻页就彻底失效。
        if kind == "page":
            timeout = to_int(self.config.get("wait_timeout_seconds"), 45)
            pending["page"] = value
            pending["expires_at"] = time.time() + timeout * 2 + 60
            source_event = pending.get("event")
            if source_event is None:
                logger.warning("[QQOfficial] 待选会话里没有 event，无法翻页")
                return
            logger.info(f"[QQOfficial] 按钮回调已翻到第 {value} 页")
            await self._send_song_page(
                source_event, pending.get("keyword", ""), pending["songs"], value
            )
            # 等待协程若还活着，同步一下页码，好让它的「下一页」文字指令接着从这页走。
            queue: asyncio.Queue | None = pending.get("queue")
            if queue is not None:
                queue.put_nowait(("page_synced", value))
            return

        queue = pending.get("queue")
        if queue is not None:
            queue.put_nowait((kind, value))
            logger.info(f"[QQOfficial] 按钮回调已选中序号 {value}")
            return

        # 等待协程已经不在了（超时/事件结束），仍然让这次选歌生效，而不是静默丢弃。
        logger.info(f"[QQOfficial] 按钮回调已选中序号 {value}（等待协程已结束，直接处理）")
        for key in candidate_keys:
            if self._pending_button_picks.get(key) is pending:
                self._pending_button_picks.pop(key, None)
        source_event = pending.get("event")
        if source_event is None:
            logger.warning("[QQOfficial] 待选会话里没有 event，无法完成选歌")
            return
        await self._handle_button_pick(source_event, pending["songs"], value)

    def _interaction_session_keys(self, interaction: Any) -> list[str]:
        keys = []
        for name in ("group_openid", "group_open_id", "user_openid", "channel_id", "guild_id"):
            value = self._clean_openid(self._dig(interaction, name))
            if value:
                keys.append(f"{name}:{value}")
        return keys

    def _selection_session_keys(self, event: AstrMessageEvent) -> list[str]:
        """和 _interaction_session_keys 对齐：interaction 里只带会话 openid，不带 AstrBot 的会话 ID。"""
        source = getattr(event.message_obj, "raw_message", None)
        keys = []
        group_openid = getattr(source, "group_openid", None)
        if group_openid:
            keys.append(f"group_openid:{group_openid}")
            keys.append(f"group_open_id:{group_openid}")
        author = getattr(source, "author", None)
        user_openid = getattr(author, "user_openid", None) if author is not None else None
        if user_openid:
            keys.append(f"user_openid:{user_openid}")
        channel_id = getattr(source, "channel_id", None)
        if channel_id:
            keys.append(f"channel_id:{channel_id}")
        return keys

    @filter.command_group("music")
    def music(self):
        pass

    @music.command("search")
    async def music_search(self, event: AstrMessageEvent, keyword: str):
        """搜索歌曲，返回候选列表并等待你发送序号。"""
        async for result in self._run_interactive_search(event, keyword, None):
            yield result

    @music.command("pick")
    async def music_pick(self, event: AstrMessageEvent, keyword: str, index: int):
        """搜索歌曲并直接选择序号。"""
        normalized_index = to_int(index, 0)
        if normalized_index < 1:
            event.stop_event()
            yield event.plain_result("请输入大于 0 的数字序号。")
            return
        async for result in self._run_interactive_search(event, keyword, normalized_index):
            yield result

    @music.command("id")
    async def music_id(self, event: AstrMessageEvent, song_id: str):
        """按歌曲 ID 直接获取歌曲信息。"""
        async for result in self._run_id_lookup(event, song_id, None):
            yield result

    @filter.command("网易点歌")
    async def netease_music(self, event: AstrMessageEvent, keyword: str):
        """使用网易云音源搜索歌曲。"""
        async for result in self._run_interactive_search(event, keyword, None, force_backend="command6"):
            yield result

    @filter.command("落月点歌")
    async def luoyue_music(self, event: AstrMessageEvent, keyword: str):
        """使用落月 API 搜索歌曲。"""
        async for result in self._run_interactive_search(event, keyword, None, force_backend="command9"):
            yield result

    @filter.command("酷狗点歌")
    async def kugou_music(self, event: AstrMessageEvent, keyword: str):
        """使用酷狗音乐搜索歌曲。"""
        async for result in self._run_interactive_search(
            event,
            keyword,
            None,
            force_backend="command9",
            force_platform="kugou",
        ):
            yield result

    async def _run_id_lookup(
        self,
        event: AstrMessageEvent,
        song_id: str,
        force_backend: str | None,
    ):
        backend = force_backend or str(self.config.get("backend", "command9"))
        try:
            detail = await self._fetch_detail(song_id, backend, None)
            async for result in self._yield_detail_results(event, detail):
                yield result
        except Exception as exc:  # noqa: BLE001
            logger.error(f"ID 点歌失败: {exc}")
            event.stop_event()
            yield event.plain_result(f"获取歌曲失败: {exc}")

    async def _run_interactive_search(
        self,
        event: AstrMessageEvent,
        keyword: str,
        direct_index: int | None,
        force_backend: str | None = None,
        force_platform: str | None = None,
    ):
        if not keyword.strip():
            event.stop_event()
            yield event.plain_result("请输入歌曲名或歌曲 ID。")
            return

        if keyword.strip().isdigit() and direct_index is None:
            async for result in self._run_id_lookup(event, keyword.strip(), force_backend):
                yield result
            return

        backend = force_backend or str(self.config.get("backend", "command9"))
        limit = to_int(self.config.get("search_list_length"), 10)
        try:
            songs = await self._search(keyword, backend, limit, force_platform=force_platform)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"搜索失败: {exc}")
            event.stop_event()
            yield event.plain_result(f"搜索失败: {exc}")
            return

        if not songs:
            event.stop_event()
            yield event.plain_result("没有找到匹配的歌曲。")
            return

        if to_bool(self.config.get("skip_song_list_selection"), False):
            direct_index = 1

        if direct_index is not None:
            direct_index = to_int(direct_index, 0)
            if direct_index < 1:
                event.stop_event()
                yield event.plain_result("请输入有效的数字序号。")
                return
            if direct_index < 1 or direct_index > len(songs):
                event.stop_event()
                yield event.plain_result(f"序号超出范围，请输入 1 到 {len(songs)}。")
                return
            try:
                detail = await self._pick_song(songs[direct_index - 1])
                async for result in self._yield_detail_results(event, detail):
                    yield result
                event.stop_event()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"直选歌曲详情失败: {exc}")
                event.stop_event()
                yield event.plain_result(f"获取歌曲详情失败: {exc}")
            return

        keyboard = await self._get_qq_keyboard_if_supported(event, songs)
        if keyboard:
            # 有按钮时正文只留标题：歌名歌手都在按钮上，不再重复列一遍歌单。
            text = self._build_button_card_text(keyword, songs, 1)
            # 先登记再发卡片：卡片一出现就可能被点，登记不能晚于它。
            self._register_button_session(event, songs, keyword)
            sent_id = await self._send_unified_qq_official_message(event, [Comp.Plain(text)], keyboard)
            self._remember_selection_card(event, sent_id)
        else:
            async for result in self._build_song_list_results(event, keyword, songs):
                yield result

        try:
            
            await self._wait_for_song_selection(event, songs, keyword)
        except TimeoutError:
            event.stop_event()
            yield event.plain_result("点歌等待超时，已结束本次选择。")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"选歌会话异常: {exc}")
            event.stop_event()
            yield event.plain_result(f"选歌过程中出现异常: {exc}")
        finally:
            event.stop_event()

    async def _search(
        self,
        keyword: str,
        backend: str,
        limit: int,
        force_platform: str | None = None,
    ) -> list[SongItem]:
        if backend == "command6":
            return await self.service.search_command6(keyword, limit)

        if force_platform:
            platforms = [force_platform]
        else:
            platforms = normalize_command9_platforms(
                self.config.get("command9_platforms") or self.config.get("command9_platform")
            )
        netease_quality = to_int(self.config.get("command9_netease_quality"), 4)
        qq_quality = to_int(self.config.get("command9_qq_quality"), 8)
        kugou_quality = str(self.config.get("command9_kugou_quality", "320"))
        return await self.service.search_command9(
            keyword,
            limit,
            platforms,
            netease_quality,
            qq_quality,
            kugou_quality,
        )

    def _parse_ai_song_request(self, raw_text: str) -> tuple[str, str | None]:
        text = str(raw_text or "").strip()
        if not text:
            return "", None

        lowered = text.lower()
        force_platform: str | None = None
        if any(keyword in lowered for keyword in ("qq音乐", "qq music", "qqmusic", "qq的", "腾讯音乐", "腾讯的", "腾讯")):
            force_platform = "tencent"
        elif any(keyword in lowered for keyword in ("酷狗音乐", "酷狗", "kugou")):
            force_platform = "kugou"
        elif any(keyword in lowered for keyword in ("网易云音乐", "网易云", "网易的", "网易", "netease")):
            force_platform = "netease"

        keyword = re.sub(r"^(来一首|点一首|放一首|播一首|搜一下|搜索|点歌)\s*", "", text, flags=re.IGNORECASE)
        keyword = re.sub(
            r"(qq音乐|qq music|qqmusic|qq|腾讯音乐|腾讯|酷狗音乐|酷狗|kugou|网易云音乐|网易云|网易)(的)?",
            "",
            keyword,
            flags=re.IGNORECASE,
        )
        keyword = re.sub(r"(歌曲|歌)\s*$", "", keyword).strip(" ，,。！？!?.")
        return keyword.strip(), force_platform

    def _format_song_candidates_for_llm(self, keyword: str, songs: list[SongItem]) -> str:
        lines = [f"搜索关键词: {keyword}", "候选歌曲列表:"]
        for index, song in enumerate(songs, start=1):
            lines.append(f"{index}. {song.name} - {song.artist}")
        lines.append("\n请选择最合适的一首，然后调用 play_song_direct 并传入对应的 candidate_index。")
        return "\n".join(lines)

    async def _build_song_list_results(
        self,
        event: AstrMessageEvent,
        keyword: str,
        songs: list[SongItem],
    ):
        """纯文字歌单，只在没有按钮的平台上使用（有按钮时走 _build_button_card_text）。"""
        exit_words = split_exit_words(self.config.get("exit_words"))
        timeout_seconds = to_int(self.config.get('wait_timeout_seconds'), 45)
        footer = f"发送序号选歌，发送 {' / '.join(exit_words)} 可退出，本次等待 {timeout_seconds} 秒。"
        render_mode = str(self.config.get("render_mode") or "text").strip().lower()
        if render_mode == "image":
            try:
                image_path = self.song_list_renderer.render(songs, keyword, self.config)
                yield event.chain_result([Comp.Image.fromFileSystem(image_path), Comp.Plain(f"\n{footer}")])
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Pillow 歌单渲染失败: {exc}")
                yield event.plain_result(f"图片歌单渲染失败，已回退为文本模式。\n\n{self._build_song_list_text(event, keyword, songs, footer)}")
                return

        yield event.plain_result(self._build_song_list_text(event, keyword, songs, footer))

    async def _wait_for_song_selection(
        self,
        event: AstrMessageEvent,
        songs: list[SongItem],
        keyword: str = "",
    ) -> None:
        exit_words = split_exit_words(self.config.get("exit_words"))
        timeout = to_int(self.config.get("wait_timeout_seconds"), 45)
        total_pages = max(1, math.ceil(len(songs) / QQ_KEYBOARD_PAGE_SIZE))
        # 指令按钮模式下「下一页」点出来是一条文字消息，这里记住当前页码好继续翻。
        state = {"page": 1}

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def waiter(controller: SessionController, next_event: AstrMessageEvent):
            text = next_event.message_str.strip().lstrip("/")
            if text in exit_words:
                await self._recall_selection_card(next_event)
                await next_event.send(next_event.plain_result("已退出点歌选择。"))
                controller.stop()
                return

            if text in NEXT_PAGE_WORDS:
                if state["page"] >= total_pages:
                    await next_event.send(next_event.plain_result("已经是最后一页了。"))
                else:
                    state["page"] += 1
                    await self._send_song_page(next_event, keyword, songs, state["page"])
                controller.keep(timeout=timeout, reset_timeout=True)
                return

            if text in PREV_PAGE_WORDS:
                if state["page"] <= 1:
                    await next_event.send(next_event.plain_result("已经是第一页了。"))
                else:
                    state["page"] -= 1
                    await self._send_song_page(next_event, keyword, songs, state["page"])
                controller.keep(timeout=timeout, reset_timeout=True)
                return

            if not text.isdigit():
                await next_event.send(next_event.plain_result("请输入数字序号，或发送退出词结束。"))
                controller.keep(timeout=timeout, reset_timeout=True)
                return

            index = int(text)
            if index < 1 or index > len(songs):
                await next_event.send(next_event.plain_result(f"序号超出范围，请输入 1 到 {len(songs)}。"))
                controller.keep(timeout=timeout, reset_timeout=True)
                return

            try:
                await self._recall_selection_card(next_event)
                detail = await self._pick_song(songs[index - 1])
                await self._send_detail_followups(next_event, detail)
            except Exception as exc:  # noqa: BLE001
                await next_event.send(next_event.plain_result(f"获取歌曲详情失败: {exc}"))
            finally:
                controller.stop()

        waiter_task = asyncio.create_task(waiter(event, session_filter=MusicSelectionSessionFilter()))

        # 回调按钮不产生消息，session_waiter 等不到它，所以另开一个队列一起等：
        # 谁先来算谁的（点按钮 / 手动发序号都能用）。翻页由回调处理里直接完成，
        # 这里只在协程活着时收一个同步通知，用来对齐「下一页」文字指令的页码。
        button_queue: asyncio.Queue | None = None
        session_keys: list[str] = []
        pending = None
        if self._use_callback_buttons() and self._supports_qq_keyboard(event):
            session_keys = self._selection_session_keys(event)
            if session_keys:
                # 发卡片时已经登记过了，这里把队列挂进同一份登记，不要覆盖成新的。
                for key in session_keys:
                    pending = self._pending_button_picks.get(key)
                    if pending is not None:
                        break
                if pending is None:
                    # 正常情况发卡片时已登记过；走到这里说明登记被清理了，补一次。
                    pending = self._register_button_session(event, songs, keyword)
                if pending is None:
                    # 连补登记都失败（拿不到会话键），退化成只用队列等待，不再登记。
                    pending = {
                        "songs": songs,
                        "keyword": keyword,
                        "event": event,
                        "page": 1,
                        "expires_at": time.time() + timeout * 2 + 60,
                    }
                button_queue = asyncio.Queue()
                pending["queue"] = button_queue

        # 只有「等到超时都没人动」才认为回调按钮不可用：用户手动发序号选完歌
        # 同样不会产生 interaction 事件，那种情况不能据此降级。
        timed_out = False
        try:
            if button_queue is None:
                await waiter_task
                return

            while True:
                get_task = asyncio.create_task(button_queue.get())
                done, _ = await asyncio.wait(
                    {waiter_task, get_task},
                    timeout=timeout + 5,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if get_task in done:
                    kind, value = get_task.result()
                    if kind == "page_synced":
                        # 翻页已在回调处理里发完卡片了，这里只对齐页码，继续等选歌。
                        state["page"] = value
                        continue
                    await self._handle_button_pick(event, songs, value)
                    return
                get_task.cancel()
                if waiter_task in done:
                    try:
                        await waiter_task  # 让 TimeoutError / 异常按原样冒泡给调用方
                    except (TimeoutError, asyncio.TimeoutError):
                        timed_out = True
                        raise
                    return
                timed_out = True
                raise TimeoutError
        finally:
            if timed_out and button_queue is not None and not self._interaction_ever_received:
                # 一次超时不足以判定回调按钮不可用：用户也可能只是没在时限内点。
                # 连续两次「发了按钮、等到超时、期间一个事件都没收到」才降级。
                self._callback_timeout_streak += 1
                if self._callback_timeout_streak >= CALLBACK_DOWNGRADE_STREAK:
                    self._callback_buttons_unavailable = True
                    logger.warning(
                        f"[music_link] 连续 {self._callback_timeout_streak} 次发出回调按钮都没收到任何"
                        "点击事件，已自动降级为指令按钮（type=2，本次运行内生效）。"
                        "根因是 interaction 事件没有推送到机器人：botpy 的 IDENTIFY 只上报适配器登录时的 "
                        "intents，缺少 interaction（1<<26）；如需零消息的回调按钮，"
                        "请在 QQ 开放平台订阅「互动事件」，或把 qq_keyboard_callback 设为 false 固定用指令按钮。"
                    )
                else:
                    logger.info(
                        f"[music_link] 本次等待超时且未收到按钮事件（{self._callback_timeout_streak}/"
                        f"{CALLBACK_DOWNGRADE_STREAK}）。仍保持回调按钮 —— 若只是没来得及点，下次照常可用。"
                    )
            elif self._interaction_ever_received:
                self._callback_timeout_streak = 0
            for key in session_keys:
                item = self._pending_button_picks.get(key)
                if item is not None and item.get("queue") is button_queue:
                    # 只摘掉队列，登记本身留给过期时间处理：等待协程结束不代表
                    # 卡片上的按钮就该失效，翻页仍然要能用（这正是之前卡在「翻页中…」的原因）。
                    item["queue"] = None
            if not waiter_task.done():
                waiter_task.cancel()
                try:
                    await waiter_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass

    def _register_button_session(
        self,
        event: AstrMessageEvent,
        songs: list[SongItem],
        keyword: str,
    ) -> dict[str, Any] | None:
        """发出按钮卡片时就登记待选会话，生命周期只由自己的过期时间决定。

        之前登记是在 _wait_for_song_selection 里做的，等待协程一结束（超时/异常/事件被
        stop_event）就把键 pop 掉，之后所有点击都只能拿到「当前登记键=[]」。翻页本身
        不需要那个协程，所以改成发卡片时登记、按过期时间清理。
        """
        if not (self._use_callback_buttons() and self._supports_qq_keyboard(event)):
            return None
        keys = self._selection_session_keys(event)
        if not keys:
            logger.warning(
                "[music_link] 取不到会话键，回调按钮点击将无法匹配（翻页/选歌会失效）。"
                f"raw_message 类型={type(getattr(event.message_obj, 'raw_message', None)).__name__}"
            )
            return None

        # 顺手清掉已过期的登记，避免长期运行下无限增长。
        now = time.time()
        for stale_key in [
            key for key, item in self._pending_button_picks.items()
            if item.get("expires_at", 0) < now
        ]:
            self._pending_button_picks.pop(stale_key, None)

        timeout = to_int(self.config.get("wait_timeout_seconds"), 45)
        pending = {
            "queue": None,          # 等待协程起来后会把自己的队列挂进来
            "songs": songs,
            "keyword": keyword,
            "event": event,
            "page": 1,
            # 翻页会不断刷新它，所以给足余量：等待超时之外再宽限一轮。
            "expires_at": now + timeout * 2 + 60,
        }
        for key in keys:
            self._pending_button_picks[key] = pending
        logger.info(f"[music_link] 已登记按钮待选会话，会话键={keys}")
        return pending

    def _remember_selection_card(self, event: AstrMessageEvent, message_id: str | None) -> None:
        """记住当前选歌卡片的消息 ID，选完歌后用来撤回它。翻页会覆盖成最新那条。"""
        if not message_id:
            return
        for key in self._selection_session_keys(event) or []:
            self._selection_card_ids[key] = message_id

    def _take_selection_card_id(self, event: AstrMessageEvent) -> str | None:
        """取出并清除当前记录的选歌卡片 ID。"""
        message_id = None
        for key in self._selection_session_keys(event) or []:
            message_id = self._selection_card_ids.pop(key, None) or message_id
        return message_id

    async def _recall_selection_card(self, event: AstrMessageEvent, reason: str = "选歌") -> None:
        """撤回本次选歌卡片（只撤一次，取完即清）。"""
        if not to_bool(self.config.get("qq_recall_after_pick"), True):
            return
        message_id = self._take_selection_card_id(event)
        if not message_id:
            return
        if await self._recall_qq_message(event, message_id):
            logger.info(f"[music_link] 已撤回{reason}卡片 message_id={message_id}")

    async def _handle_button_pick(
        self,
        event: AstrMessageEvent,
        songs: list[SongItem],
        index: int,
    ) -> None:
        # 选完就注销登记：卡片已经撤了，残留的登记只会让过期按钮继续生效。
        for key in self._selection_session_keys(event) or []:
            self._pending_button_picks.pop(key, None)
        # 先撤回选歌卡片：点完就收起来，避免过期按钮留在聊天里被反复点。
        await self._recall_selection_card(event)
        if index < 1 or index > len(songs):
            await event.send(event.plain_result("已退出点歌选择。"))
            return
        try:
            detail = await self._pick_song(songs[index - 1])
            await self._send_detail_followups(event, detail)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"按钮选歌失败: {exc}")
            await event.send(event.plain_result(f"获取歌曲详情失败: {exc}"))

    async def _send_song_menu_and_wait(
        self,
        event: AstrMessageEvent,
        keyword: str,
        force_platform: str | None,
    ) -> None:
        backend = "command9" if force_platform else str(self.config.get("backend", "command9"))
        limit = to_int(self.config.get("search_list_length"), 10)
        try:
            songs = await self._search(keyword, backend, limit, force_platform=force_platform)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AI 点歌搜索失败: {exc}")
            await event.send(event.plain_result(f"搜索失败: {exc}"))
            return

        if not songs:
            await event.send(event.plain_result(f"没有找到与“{keyword}”匹配的歌曲。"))
            return

        try:
            keyboard = await self._get_qq_keyboard_if_supported(event, songs)
            if keyboard:
                # 有按钮时正文只留标题，不再重复列歌单。
                text = self._build_button_card_text(keyword, songs, 1)
                # 先登记再发卡片：卡片一出现就可能被点，登记不能晚于它。
                self._register_button_session(event, songs, keyword)
                sent_id = await self._send_unified_qq_official_message(event, [Comp.Plain(text)], keyboard)
                self._remember_selection_card(event, sent_id)
            else:
                async for result in self._build_song_list_results(event, keyword, songs):
                    await event.send(result)
            await self._wait_for_song_selection(event, songs, keyword)
        except TimeoutError:
            await event.send(event.plain_result("点歌等待超时，已结束本次选择。"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AI 点歌会话异常: {exc}")
            await event.send(event.plain_result(f"点歌过程中出现异常: {exc}"))
        finally:
            event.stop_event()

    async def _search_song_candidates_for_llm(
        self,
        keyword: str,
        force_platform: str | None,
    ) -> str:
        backend = "command9" if force_platform else str(self.config.get("backend", "command9"))
        limit = max(1, to_int(self.config.get("search_list_length"), 10))
        songs = await self._search(keyword, backend, limit, force_platform=force_platform)
        if not songs:
            return f"没有找到与“{keyword}”匹配的歌曲。"
        return self._format_song_candidates_for_llm(keyword, songs)

    async def _send_song_by_index(
        self,
        event: AstrMessageEvent,
        keyword: str,
        force_platform: str | None,
        candidate_index: int,
    ) -> None:
        backend = "command9" if force_platform else str(self.config.get("backend", "command9"))
        limit = max(1, to_int(self.config.get("search_list_length"), 10))
        try:
            songs = await self._search(keyword, backend, limit, force_platform=force_platform)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AI 直出点歌搜索失败: {exc}")
            await event.send(event.plain_result(f"搜索失败: {exc}"))
            return

        if not songs:
            await event.send(event.plain_result(f"没有找到与“{keyword}”匹配的歌曲。"))
            return

        if candidate_index < 1 or candidate_index > len(songs):
            await event.send(event.plain_result(f"候选序号超出范围，请选择 1 到 {len(songs)}。"))
            return

        try:
            selected_song = songs[candidate_index - 1]
            detail = await self._pick_song(selected_song)
            await self._send_detail_followups(event, detail)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AI 直出点歌失败: {exc}")
            await event.send(event.plain_result(f"发送歌曲失败: {exc}"))
        finally:
            event.stop_event()

    async def _fetch_detail(
        self,
        song_id: str,
        backend: str,
        platform: str | None,
        song_raw: dict[str, Any] | None = None,
        keyword: str = "",
    ) -> SongDetail:
        if backend == "command6":
            api_url = first_config_url(
                self.config,
                "command6_api_urls",
                "command6_used_api",
                COMMAND6_API_URLS["api.injahow.cn"],
            )
            detail = await self.service.get_command6_detail(song_id, api_url)
        else:
            configured_platforms = normalize_command9_platforms(
                self.config.get("command9_platforms") or self.config.get("command9_platform")
            )
            if platform is None and (len(configured_platforms) > 1 or configured_platforms[0] == "kugou"):
                raise ValueError("多平台或酷狗模式不支持 ID 直点，请先搜索后选择歌曲")
            final_platform = platform or configured_platforms[0]
            if final_platform == "netease":
                quality: int | str = to_int(self.config.get("command9_netease_quality"), 4)
            elif final_platform == "kugou":
                quality = str(self.config.get("command9_kugou_quality", "320"))
            else:
                quality = to_int(self.config.get("command9_qq_quality"), 8)
            detail = await self.service.get_command9_detail(
                song_id,
                final_platform,
                quality,
                song_raw,
                keyword,
            )

        max_duration = to_int(self.config.get("max_duration_seconds"), 1800)
        if detail.duration_seconds > max_duration:
            raise ValueError(f"歌曲时长 {detail.duration_seconds}s 超出限制 {max_duration}s")
        return detail

    async def _pick_song(self, song: SongItem) -> SongDetail:
        return await self._fetch_detail(
            song.song_id,
            song.source_backend,
            song.platform,
            song.raw,
            song.name,
        )

    def _song_list_source_suffix(self, songs: list[SongItem]) -> str:
        """歌单里出现过的来源标签，形如 “（网易云）”；多来源合并，无来源时返回空串。"""
        labels: list[str] = []
        for song in songs:
            label = song.platform_label or PLATFORM_LABELS.get(song.platform, song.platform)
            if label and label not in labels:
                labels.append(label)
        return f"（{' / '.join(labels)}）" if labels else ""

    def _build_song_list_text(
        self,
        event: AstrMessageEvent,
        keyword: str,
        songs: list[SongItem],
        footer: str,
    ) -> str:
        """纯文字歌单，给没有按钮的平台用（QQ 官方按钮卡片走 _build_button_card_text）。"""
        source_suffix = self._song_list_source_suffix(songs)
        if self._supports_markdown(event):
            lines = [f"# 🔍 点歌结果: {keyword}{source_suffix}", ""]
            for index, song in enumerate(songs, start=1):
                # 形如 "1.歌名-歌手"，点击文字也能选歌
                lines.append(f"[{index}.{song.name}-{song.artist}](cmd:{index})")
            lines.append("")
            lines.append("[0.退出点歌](cmd:0)")
        else:
            # OneBot 等不渲染 markdown 的平台：去掉标题符号和链接包装，靠序号选歌
            lines = [f"🔍 点歌结果: {keyword}{source_suffix}", ""]
            for index, song in enumerate(songs, start=1):
                lines.append(f"{index}.{song.name}-{song.artist}")
            lines.append("")
            lines.append("0.退出点歌")
        lines.append("")
        lines.append(footer)
        return "\n".join(lines)

    def _build_button_card_text(self, keyword: str, songs: list[SongItem], page: int = 1) -> str:
        """按钮卡片的正文：只留标题，歌名歌手交给按钮显示，避免同一份歌单出现两遍。

        QQ 的 markdown.content 不能为空，所以至少保留这一行标题。
        """
        total_pages = max(1, math.ceil(len(songs) / QQ_KEYBOARD_PAGE_SIZE))
        page = max(1, min(page, total_pages))
        title = f"### 🔍 {keyword} 请选歌{self._song_list_source_suffix(songs)}："
        if total_pages > 1:
            title = f"{title}（第 {page}/{total_pages} 页）"
        return title

    async def _send_song_page(
        self,
        event: AstrMessageEvent,
        keyword: str,
        songs: list[SongItem],
        page: int,
    ) -> None:
        """翻页：发出该页按钮卡片，并撤回上一页那条，聊天里始终只留一张卡片。"""
        keyboard = await self._get_qq_keyboard_if_supported(event, songs, page)
        if not keyboard:
            return
        # 先取出上一页的卡片 ID，等新卡片发出去之后再撤，避免中间出现无按钮的空窗。
        previous_id = self._take_selection_card_id(event)
        text = self._build_button_card_text(keyword, songs, page)
        sent_id = await self._send_unified_qq_official_message(event, [Comp.Plain(text)], keyboard)
        self._remember_selection_card(event, sent_id)
        if (
            previous_id
            and previous_id != sent_id
            and to_bool(self.config.get("qq_recall_after_pick"), True)
        ):
            await self._recall_qq_message(event, previous_id)

    async def terminate(self):
        logger.info("astrbot_plugin_music_link 已卸载")



