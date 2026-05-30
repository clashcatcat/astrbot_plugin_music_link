from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
import math
import json
import mimetypes
import re
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
from astrbot.core.utils.session_waiter import SessionController, session_waiter


PLUGIN_VERSION = "1.0.0"


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
        platform_label = "网易云" if self.platform == "netease" else "QQ音乐"
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


def truncate_text(text: Any, max_length: int) -> str:
    value = str(text or "")
    if len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."


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
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        logger.warning("output_fields_json 不是合法 JSON，回退默认字段配置")
        return DEFAULT_OUTPUT_FIELDS
    if isinstance(parsed, list):
        valid_items = [item for item in parsed if isinstance(item, dict)]
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
        show_dividers = to_bool(config.get("svg_show_dividers"), True)
        show_background = to_bool(config.get("svg_show_song_background"), True)
        show_version = to_bool(config.get("svg_show_version_info"), True)
        scale = max(1.0, float(config.get("svg_scale", 1.8)))

        canvas_width = int(width * scale)
        padding = int(18 * scale)
        header_height = int(62 * scale)
        item_height = int(58 * scale)
        footer_height = int((56 if show_version else 26) * scale)
        songs_per_col = max(1, math.ceil(len(songs) / columns))
        total_height = header_height + songs_per_col * item_height + footer_height + padding * 2
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
            col = index % columns
            row = index // columns
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
                tags.append(("网易" if song.platform == "netease" else "QQ", "#ffffff", "#e4393c" if song.platform == "netease" else "#31c27c"))
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
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/STHeiti Light.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
                "/Library/Fonts/Arial Unicode.ttf",
            ]
        )
        for candidate in candidates:
            try:
                if candidate and Path(candidate).exists():
                    return image_font_module.truetype(candidate, size=size)
            except Exception:
                continue
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

    async def search_command6(self, keyword: str, limit: int) -> list[SongItem]:
        api = (
            "http://music.163.com/api/search/get/web"
            f"?csrf_token=hlpretag=&hlposttag=&s={quote(keyword)}"
            f"&type=1&offset=0&total=true&limit={limit}"
        )
        data = await self._get_json(api)
        songs = ((data or {}).get("result") or {}).get("songs") or []
        items: list[SongItem] = []
        for song in songs:
            artists = "/".join(artist.get("name", "") for artist in song.get("artists", []))
            duration_seconds = to_int(song.get("duration"), 0) // 1000
            album = (song.get("album") or {}).get("name", "")
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
                    cover=(song.get("album") or {}).get("picUrl", ""),
                    quality="",
                    raw=song,
                )
            )
        return items

    async def get_command6_detail(self, song_id: str, use_api: str) -> SongDetail:
        song_url_map = {
            "api.injahow.cn": f"https://api.injahow.cn/meting/?type=url&id={song_id}",
            "meting.jmstrand.cn": f"https://meting.jmstrand.cn/?type=url&id={song_id}",
            "api.qijieya.cn": f"https://api.qijieya.cn/meting/?type=url&id={song_id}",
            "metingapi.nanorocky.top": f"https://metingapi.nanorocky.top/?server=netease&type=url&id={song_id}",
        }
        detail_api = f"http://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
        lyric_api = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"

        detail_data = await self._get_json(detail_api)
        songs = (detail_data or {}).get("songs") or []
        if not songs:
            raise ValueError("网易云歌曲详情为空")
        song = songs[0]

        lyric_text = ""
        try:
            lyric_data = await self._get_json(lyric_api)
            lyric_text = ((lyric_data or {}).get("lrc") or {}).get("lyric", "") or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取网易云歌词失败: {exc}")

        song_url = song_url_map.get(use_api)
        if not song_url:
            raise ValueError(f"不支持的 command6 直链后端: {use_api}")

        artists = "/".join(artist.get("name", "") for artist in song.get("artists", []))
        duration_seconds = to_int(song.get("duration"), 0) // 1000
        album_data = song.get("album") or {}
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

    async def search_command9(
        self,
        keyword: str,
        limit: int,
        platform: str,
        netease_quality: int,
        qq_quality: int,
    ) -> list[SongItem]:
        base_url = str(self.config.get("luoyue_api_base_url", "https://api.vkeys.cn")).rstrip("/")
        if platform == "aggregation":
            half_limit = max(1, limit // 2)
            netease_url = (
                f"{base_url}/v2/music/netease?word={quote(keyword)}"
                f"&num={half_limit}&quality={netease_quality}"
            )
            qq_url = (
                f"{base_url}/v2/music/tencent?word={quote(keyword)}"
                f"&num={half_limit}&quality={qq_quality}"
            )
            netease_resp, qq_resp = await self._gather_json(netease_url, qq_url)
            netease_items = self._normalize_command9_items(netease_resp, "netease", "网易云")
            qq_items = self._normalize_command9_items(qq_resp, "tencent", "QQ音乐")
            merged: list[SongItem] = []
            max_len = max(len(netease_items), len(qq_items))
            for index in range(max_len):
                if index < len(netease_items):
                    merged.append(netease_items[index])
                if index < len(qq_items):
                    merged.append(qq_items[index])
            return merged[:limit]

        quality = netease_quality if platform == "netease" else qq_quality
        url = (
            f"{base_url}/v2/music/{platform}?word={quote(keyword)}"
            f"&num={limit}&quality={quality}"
        )
        data = await self._get_json(url)
        platform_label = "网易云" if platform == "netease" else "QQ音乐"
        return self._normalize_command9_items(data, platform, platform_label)

    async def _gather_json(self, *urls: str) -> tuple[Any, ...]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            responses = await asyncio.gather(*[client.get(url) for url in urls])
            results: list[Any] = []
            for response in responses:
                response.raise_for_status()
                results.append(response.json())
            return tuple(results)

    def _normalize_command9_items(self, payload: Any, platform: str, platform_label: str) -> list[SongItem]:
        if not payload or payload.get("code") != 200 or not payload.get("data"):
            return []
        raw_items = payload["data"] if isinstance(payload["data"], list) else [payload["data"]]
        items: list[SongItem] = []
        for raw in raw_items:
            duration_seconds = parse_cn_duration(raw.get("interval"))
            items.append(
                SongItem(
                    song_id=str(raw.get("id", "")),
                    source_backend="command9",
                    platform=platform,
                    platform_label=platform_label,
                    name=raw.get("song") or raw.get("name") or "未知歌曲",
                    artist=raw.get("singer") or raw.get("artist") or "未知歌手",
                    album=raw.get("album") or "",
                    duration_seconds=duration_seconds,
                    duration_text=format_duration(duration_seconds),
                    cover=raw.get("cover") or raw.get("pic") or "",
                    quality=raw.get("quality") or "",
                    raw=raw,
                )
            )
        return items

    async def get_command9_detail(self, song_id: str, platform: str, quality: int) -> SongDetail:
        base_url = str(self.config.get("luoyue_api_base_url", "https://api.vkeys.cn")).rstrip("/")
        detail_api = f"{base_url}/v2/music/{platform}?id={song_id}&quality={quality}"
        lyric_api = f"{base_url}/v2/music/{platform}/lyric?id={song_id}"

        detail_data = await self._get_json(detail_api)
        if not detail_data or detail_data.get("code") != 200 or not detail_data.get("data"):
            raise ValueError("落月 API 返回空详情")
        raw = detail_data["data"]

        lyric_text = ""
        try:
            lyric_data = await self._get_json(lyric_api)
            lyric_text = ((lyric_data or {}).get("data") or {}).get("lrc", "") or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取落月 API 歌词失败: {exc}")

        duration_seconds = parse_cn_duration(raw.get("interval"))
        return SongDetail(
            song_id=str(raw.get("id", "")),
            platform=platform,
            name=raw.get("song") or raw.get("name") or "未知歌曲",
            artist=raw.get("singer") or raw.get("artist") or "未知歌手",
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


@register("astrbot_plugin_music_link", "VincentZyuApps / Codex", "AstrBot 点歌插件", "1.0.0")
class MusicLinkPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = MusicService(config)
        self.song_list_renderer = LocalSongListRenderer("astrbot_plugin_music_link")
        self.context.add_llm_tools(PlaySongMenuTool(plugin=self))

    async def initialize(self):
        logger.info("astrbot_plugin_music_link 已初始化")

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
        detail: SongDetail,
    ) -> tuple[list[Comp.BaseMessageComponent], list[list[Comp.BaseMessageComponent]]]:
        field_map = detail.as_field_map()
        specs = parse_output_fields(
            self.config.get("output_fields") or self.config.get("output_fields_json")
        )
        components: list[Comp.BaseMessageComponent] = []
        deferred_messages: list[list[Comp.BaseMessageComponent]] = []
        text_lines: list[str] = []
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
                text_lines.append(f"{label}: {text_value}")
                continue

            if text_lines:
                components.append(Comp.Plain("\n".join(text_lines)))
                text_lines = []

            if field_type == "image":
                components.append(Comp.Image.fromURL(value))
            elif field_type == "audio":
                if field_name == "url" and keep_url_text_for_media and not explicit_url_text_enabled:
                    text_lines.append(f"{label}: {value}")
                audio_message = [Comp.Record.fromURL(value)]
                if separate_media:
                    deferred_messages.append(audio_message)
                else:
                    components.extend(audio_message)
            elif field_type == "file":
                if field_name == "url" and keep_url_text_for_media and not explicit_url_text_enabled:
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
        primary_components, deferred_messages = await self._build_detail_components(detail)
        yield event.chain_result(self._wrap_primary_with_forward(event, primary_components))
        for message in deferred_messages:
            yield event.chain_result(message)
        await self._send_music_card(event, detail)

    async def _send_detail_followups(self, event: AstrMessageEvent, detail: SongDetail) -> None:
        primary_components, deferred_messages = await self._build_detail_components(detail)
        await event.send(event.chain_result(self._wrap_primary_with_forward(event, primary_components)))
        for message in deferred_messages:
            await event.send(event.chain_result(message))
        await self._send_music_card(event, detail)

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
        async for result in self._run_interactive_search(event, keyword, index):
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
            yield event.plain_result(f"搜索失败: {exc}")
            return

        if not songs:
            yield event.plain_result("没有找到匹配的歌曲。")
            return

        if to_bool(self.config.get("skip_song_list_selection"), False):
            direct_index = 1

        if direct_index is not None:
            if direct_index < 1 or direct_index > len(songs):
                yield event.plain_result(f"序号超出范围，请输入 1 到 {len(songs)}。")
                return
            detail = await self._pick_song(songs[direct_index - 1])
            async for result in self._yield_detail_results(event, detail):
                yield result
            return

        async for result in self._build_song_list_results(event, keyword, songs):
            yield result

        try:
            await self._wait_for_song_selection(event, songs)
        except TimeoutError:
            yield event.plain_result("点歌等待超时，已结束本次选择。")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"选歌会话异常: {exc}")
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

        platform = force_platform or str(self.config.get("command9_platform", "aggregation"))
        netease_quality = to_int(self.config.get("command9_netease_quality"), 4)
        qq_quality = to_int(self.config.get("command9_qq_quality"), 8)
        return await self.service.search_command9(keyword, limit, platform, netease_quality, qq_quality)

    def _parse_ai_song_request(self, raw_text: str) -> tuple[str, str | None]:
        text = str(raw_text or "").strip()
        if not text:
            return "", None

        lowered = text.lower()
        force_platform: str | None = None
        if any(keyword in lowered for keyword in ("qq音乐", "qq music", "qqmusic", "qq的", "腾讯音乐", "腾讯的", "腾讯")):
            force_platform = "tencent"
        elif any(keyword in lowered for keyword in ("网易云音乐", "网易云", "网易的", "网易", "netease")):
            force_platform = "netease"

        keyword = re.sub(r"^(来一首|点一首|放一首|播一首|搜一下|搜索|点歌)\s*", "", text, flags=re.IGNORECASE)
        keyword = re.sub(
            r"(qq音乐|qq music|qqmusic|qq|腾讯音乐|腾讯|网易云音乐|网易云|网易)(的)?",
            "",
            keyword,
            flags=re.IGNORECASE,
        )
        keyword = re.sub(r"(歌曲|歌)\s*$", "", keyword).strip(" ，,。！？!?.")
        return keyword.strip(), force_platform

    async def _build_song_list_results(
        self,
        event: AstrMessageEvent,
        keyword: str,
        songs: list[SongItem],
    ):
        exit_words = split_exit_words(self.config.get("exit_words"))
        footer = f"发送序号选歌，发送 {' / '.join(exit_words)} 可退出，本次等待 {to_int(self.config.get('wait_timeout_seconds'), 45)} 秒。"
        render_mode = str(self.config.get("render_mode", "text"))
        if render_mode == "image":
            try:
                image_path = self.song_list_renderer.render(songs, keyword, self.config)
                yield event.chain_result([Comp.Image.fromFileSystem(image_path), Comp.Plain(f"\n{footer}")])
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Pillow 歌单渲染失败: {exc}")
                yield event.plain_result(f"图片歌单渲染失败，已回退为文本模式。\n\n{self._build_song_list_text(keyword, songs, footer)}")
                return

        yield event.plain_result(self._build_song_list_text(keyword, songs, footer))

    async def _wait_for_song_selection(
        self,
        event: AstrMessageEvent,
        songs: list[SongItem],
    ) -> None:
        exit_words = split_exit_words(self.config.get("exit_words"))
        timeout = to_int(self.config.get("wait_timeout_seconds"), 45)

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def waiter(controller: SessionController, next_event: AstrMessageEvent):
            text = next_event.message_str.strip()
            if text in exit_words:
                await next_event.send(next_event.plain_result("已退出点歌选择。"))
                controller.stop()
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
                detail = await self._pick_song(songs[index - 1])
                await self._send_detail_followups(next_event, detail)
            except Exception as exc:  # noqa: BLE001
                await next_event.send(next_event.plain_result(f"获取歌曲详情失败: {exc}"))
            finally:
                controller.stop()

        await waiter(event)

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
            async for result in self._build_song_list_results(event, keyword, songs):
                await event.send(result)
            await self._wait_for_song_selection(event, songs)
        except TimeoutError:
            await event.send(event.plain_result("点歌等待超时，已结束本次选择。"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AI 点歌会话异常: {exc}")
            await event.send(event.plain_result(f"点歌过程中出现异常: {exc}"))
        finally:
            event.stop_event()

    async def _fetch_detail(self, song_id: str, backend: str, platform: str | None) -> SongDetail:
        if backend == "command6":
            use_api = str(self.config.get("command6_used_api", "api.injahow.cn"))
            detail = await self.service.get_command6_detail(song_id, use_api)
        else:
            final_platform = platform or str(self.config.get("command9_platform", "netease"))
            if final_platform == "aggregation":
                final_platform = "netease"
            quality = (
                to_int(self.config.get("command9_netease_quality"), 4)
                if final_platform == "netease"
                else to_int(self.config.get("command9_qq_quality"), 8)
            )
            detail = await self.service.get_command9_detail(song_id, final_platform, quality)

        max_duration = to_int(self.config.get("max_duration_seconds"), 1800)
        if detail.duration_seconds > max_duration:
            raise ValueError(f"歌曲时长 {detail.duration_seconds}s 超出限制 {max_duration}s")
        return detail

    async def _pick_song(self, song: SongItem) -> SongDetail:
        return await self._fetch_detail(song.song_id, song.source_backend, song.platform)

    def _build_song_list_text(self, keyword: str, songs: list[SongItem], footer: str) -> str:
        lines = [f"点歌结果: {keyword}"]
        for index, song in enumerate(songs, start=1):
            tail = []
            if song.platform_label:
                tail.append(song.platform_label)
            if song.quality:
                tail.append(song.quality)
            if song.duration_text:
                tail.append(song.duration_text)
            summary = " | ".join(tail)
            album = f" | 专辑: {song.album}" if song.album else ""
            lines.append(f"{index}. {song.name} - {song.artist}{album}{(' | ' + summary) if summary else ''}")
        lines.append("")
        lines.append(footer)
        return "\n".join(lines)

    async def terminate(self):
        logger.info("astrbot_plugin_music_link 已卸载")
