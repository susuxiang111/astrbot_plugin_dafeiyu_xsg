"""大肥鱼 · 新三国梗

只认 2010 版电视剧《新三国》（高希希执导）衍生的梗，以及 B 站 UP 主
「吃蛋挞的折棒」《吐槽新三国》系列的二创梗与评论区梗。

梗分三层，回答出处时必须区分：
  original  原剧台词/场景
  zhebang   折棒吐槽系列总结的机制（二创，不是原剧）
  comment   折棒系列评论区梗

行为：
  - 默认接梗：识别到梗就在本轮上下文里塞好「这是什么 + 怎么接」，让主模型自然接住
  - 查梗模式：用户说「查梗/什么梗/出处/详细/展开」时，按【梗名】【一句话】【出处】短答
  - 多轮：记住上一轮讲的梗，用户说「详细/还有吗」接着讲
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

NAME = "astrbot_plugin_dafeiyu_xsg"
DATA_PATH = Path(__file__).resolve().parent / "memes.json"

LAYER_LABEL = {
    "original": "原剧台词/场景",
    "zhebang": "折棒吐槽系列总结的机制",
    "comment": "折棒系列评论区梗",
}

# 只有这些词出现，才算用户在问「出处/详情」，才进查梗模式
DETAIL_ASK = re.compile(
    r"(?:查梗|梗百科|什么梗|啥梗|什么意思|啥意思|什么出处|哪来的|出处|详细|展开|"
    r"讲讲|说说这个|这梗|什么来头|为什么成梗|怎么来的|是什么|干啥的|干嘛的|介绍一下)"
)
DETAIL_FOLLOWUP = re.compile(r"^(?:详细|展开|还有吗|还有呢|继续|再多说点|说详细点)[。！!？?~～]*$")
LIST_ASK = re.compile(r"^(?:/\s*)?(?:列表|梗列表|全部梗|有哪些梗|新三国梗列表)$")

# 判断「他是不是在打听这个梗」，而不是顺口提到。
# 命中梗之后再看这条：像在问就直出卡片，只是提一嘴就交给模型自然接。
QUERY_HINT = re.compile(
    r"(?:查一?下?|搜一?下?|什么梗|啥梗|什么意思|啥意思|什么来头|什么出处|"
    r"出处|哪来的|怎么来的|为什么成梗|详细|展开|讲讲|说说|解释|介绍一下|科普)"
)

# 接梗时的行为约束，跟梗条目一起注入
RULES = (
    "[新三国梗·内置用法]\n"
    "1. 上面这些梗只作背景知识：用户说了就接着聊，别念百科、别主动科普；\n"
    "2. 只有他明确问「什么梗/出处/详细」才解释，一两句话说完；\n"
    "3. 出处分三层：原剧的就说原剧，折棒总结的就说折棒总结的，评论区的就说评论区的，别混；\n"
    "4. 只认 2010 版《新三国》和折棒体系；老版三国演义、三国正史、军师联盟的梗别硬套；\n"
    "5. 拿不准就说「不确定是不是这个」，绝不编台词、集数和出处。"
)


def _clean(value: object, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] + "…" if len(text) > limit else text


def load_entries() -> list[dict[str, Any]]:
    if not DATA_PATH.exists():
        logger.warning(f"[{NAME}] 找不到梗库 {DATA_PATH}")
        return []
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception as error:
        logger.error(f"[{NAME}] 梗库解析失败: {error!r}")
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return []
    cleaned = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"), 40)
        if not name:
            continue
        layer = str(item.get("layer") or "original").strip()
        if layer not in LAYER_LABEL:
            layer = "original"
        cleaned.append({
            "name": name,
            "aliases": [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()],
            "layer": layer,
            "one_line": _clean(item.get("one_line"), 120),
            "explain": _clean(item.get("explain"), 500),
            "example": _clean(item.get("example"), 200),
            "quote": _clean(item.get("quote"), 200),
            "character": _clean(item.get("character"), 20),
            "confidence": str(item.get("confidence") or "high"),
            "note": _clean(item.get("note"), 200),
            "source_note": _clean(item.get("source_note"), 200),
        })
    return cleaned


class MemeBook:
    """梗库索引：负责识别和查询。"""

    def __init__(self, entries: list[dict[str, Any]]):
        self.entries = entries
        self.by_name: dict[str, dict[str, Any]] = {}
        self.triggers: list[tuple[str, dict[str, Any]]] = []
        for entry in entries:
            self.by_name.setdefault(entry["name"], entry)
            keys = {entry["name"], *entry["aliases"]}
            # 台词只取前 12 字做触发，太长容易误匹配
            quote = entry.get("quote") or ""
            if 4 <= len(quote) <= 24:
                keys.add(quote)
            for key in keys:
                key = key.strip()
                # 单字和超短词不参与自动识别，误伤太狠（比如「巨」）
                if len(key) < 2:
                    continue
                self.triggers.append((key, entry))
        # 长词优先，避免「接着奏乐」被「奏乐」抢走
        self.triggers.sort(key=lambda pair: len(pair[0]), reverse=True)

    def identify(self, text: str, limit: int = 3) -> list[dict[str, Any]]:
        value = text or ""
        if not value.strip():
            return []
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key, entry in self.triggers:
            if entry["name"] in seen:
                continue
            if key in value:
                # 标点、空格隔开的写法规避误伤：「巨」这类已经在建立索引时排除
                found.append(entry)
                seen.add(entry["name"])
                if len(found) >= limit:
                    break
        return found

    def lookup(self, query: str) -> dict[str, Any] | None:
        value = re.sub(r"^[\s/]+", "", (query or "").strip())
        value = value.strip(" 　，,。.！!？?:：")
        if not value:
            return None
        if value in self.by_name:
            return self.by_name[value]
        for entry in self.entries:
            if value in entry["aliases"]:
                return entry
        # 包含匹配：先找「查询是梗名的一部分」，再找「梗名是查询的一部分」
        candidates = [e for e in self.entries if value in e["name"] or e["name"] in value]
        if candidates:
            return sorted(candidates, key=lambda e: len(e["name"]))[0]
        for entry in self.entries:
            if any(value in alias for alias in entry["aliases"]):
                return entry
        # 台词检索
        for entry in self.entries:
            quote = entry.get("quote") or ""
            if quote and (value in quote or quote in value):
                return entry
        return None

    def by_layer(self, layer: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["layer"] == layer]


def render_card(entry: dict[str, Any], detail: bool = False) -> str:
    """查梗模式的短答；detail=True 时补全说明。"""
    label = LAYER_LABEL.get(entry["layer"], "来源不明")
    lines = [
        f"【梗名】{entry['name']}",
        f"【一句话】{entry['one_line'] or entry['explain'][:60]}",
        f"【出处】{label}",
    ]
    if entry.get("confidence") == "low":
        lines.append("（这条说法还没核实）")
    if detail:
        if entry.get("explain"):
            lines.append(f"【为什么成梗】{entry['explain']}")
        if entry.get("example"):
            lines.append(f"【怎么用】{entry['example']}")
        related = entry.get("related") or []
        if related:
            lines.append(f"【相关梗】{'、'.join(related)}")
        # 三层的出处细节
        if entry["layer"] == "original" and entry.get("quote"):
            lines.append(f"【原剧】{entry['quote']}")
        elif entry["layer"] == "zhebang":
            lines.append("【说明】这是折棒吐槽里总结的机制，不是原剧台词")
        elif entry["layer"] == "comment":
            lines.append("【说明】网友在评论区刷出来的，不是官方设定")
        if entry.get("note"):
            lines.append(f"【备注】{entry['note']}")
    return "\n".join(lines)


def render_context(entries: list[dict[str, Any]], ask_detail: bool) -> str:
    """接梗模式注入的上下文块。"""
    blocks = []
    for entry in entries:
        label = LAYER_LABEL.get(entry["layer"], "来源不明")
        body = [f"「{entry['name']}」（{label}）"]
        if entry["one_line"]:
            body.append(f"  是什么：{entry['one_line']}")
        if entry.get("explain"):
            body.append(f"  细一点：{_clean(entry['explain'], 260)}")
        if entry.get("example"):
            body.append(f"  怎么接：{entry['example']}")
        if entry.get("confidence") == "low":
            body.append("  （这条说法没核实，别当准的讲）")
        blocks.append("\n".join(body))
    return RULES + "\n\n[用户这轮提到的梗]\n" + "\n\n".join(blocks)


@register(NAME, "dafeiyu", "新三国梗百科：143 条新三国与折棒系梗，三层出处分得清，说人话就能查。", "1.0.0")
class DafeiyuXsg(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.book = MemeBook(load_entries())
        self._last: dict[str, str] = {}
        logger.info(f"[{NAME}] 梗库载入 {len(self.book.entries)} 条")

    # ---------- 配置 ----------

    def _auto(self) -> bool:
        return bool(self.conf.get("auto_recognize", True))

    def _group_need_trigger(self) -> bool:
        return bool(self.conf.get("group_need_trigger", True))

    # ---------- 会话记忆 ----------

    def _key(self, event: AstrMessageEvent) -> str:
        return f"{event.get_group_id() or 'private'}:{event.get_sender_id()}"

    def _remember(self, event: AstrMessageEvent, entry: dict[str, Any]) -> None:
        self._last[self._key(event)] = entry["name"]

    def _recall(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        name = self._last.get(self._key(event))
        return self.book.by_name.get(name) if name else None

    # ---------- 群聊门禁 ----------

    def _allowed_in_group(self, event: AstrMessageEvent, text: str) -> bool:
        """群聊默认只在被叫到时接梗，避免刷屏。"""
        if event.is_private_chat():
            return True
        if not self._group_need_trigger():
            return True
        if getattr(event, "is_at_or_wake_command", False):
            return True
        message = getattr(event, "message_obj", None)
        if message is not None and getattr(message, "self_id", None):
            # 被回复也算叫到
            raw = str(getattr(message, "raw_message", "") or "")
            if "[CQ:reply" in raw:
                return True
        return bool(DETAIL_ASK.search(text)) or text.lstrip().startswith("/")

    def _strict_called_in_group(self, event: AstrMessageEvent, text: str) -> bool:
        """自然语言直出卡片时更严：群里必须被 @、被回复，或带明确命令式触发词。"""
        if event.is_private_chat():
            return True
        if not self._group_need_trigger():
            return True
        if getattr(event, "is_at_or_wake_command", False):
            return True
        message = getattr(event, "message_obj", None)
        if message is not None and getattr(message, "self_id", None):
            raw = str(getattr(message, "raw_message", "") or "")
            if "[CQ:reply" in raw:
                return True
        # 只有「查梗 / 梗百科 / 新三梗」这类明确点名的说法才在群里直出
        return bool(re.search(r"(?:查梗|梗百科|新三国梗|新三梗|折棒梗)", text))

    # ---------- 自然语言查梗：不用打 / 也能直出卡片 ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def natural_lookup(self, event: AstrMessageEvent):
        """用户直接说话问梗时，跳过模型，直接回结构化的梗卡片。"""
        if not bool(self.conf.get("natural_lookup", True)):
            return
        text = (event.get_message_str() or "").strip()
        if not text or text.startswith("/") or not self.book.entries:
            return
        # 「详细」这类追问交给 on_llm_request 处理（那里有会话记忆）
        if DETAIL_FOLLOWUP.fullmatch(text):
            return
        if not self._allowed_in_group(event, text):
            return
        if not self._strict_called_in_group(event, text):
            return
        hits = self.book.identify(text, limit=3)
        if not hits:
            return
        # 只是顺口提到梗 → 不抢话，交给模型自然接
        if not QUERY_HINT.search(text):
            return
        entry = hits[0]
        self._remember(event, entry)
        detail = len(hits) == 1 or len(text) <= 24
        yield event.plain_result(render_card(entry, detail=detail))
        logger.info(f"[{NAME}] 自然语言查梗直出：{entry['name']}")
        event.stop_event()

    # ---------- 接梗：注入上下文 ----------

    @filter.on_llm_request(priority=-1100)
    async def inject_memes(self, event: AstrMessageEvent, req):
        if not self._auto() or not self.book.entries:
            return
        text = event.get_message_str() or ""
        if not text.strip():
            return
        if not self._allowed_in_group(event, text):
            return

        # 「详细/展开」这类追问，直接回上一个梗
        if DETAIL_FOLLOWUP.fullmatch(text.strip()):
            entry = self._recall(event)
            if entry is not None:
                req.system_prompt = (req.system_prompt or "") + "\n\n" + (
                    "[用户要上一个梗的详情，按这个讲：]\n"
                    + render_card(entry, detail=True)
                )
                logger.info(f"[{NAME}] 追加上一个梗详情：{entry['name']}")
            return

        hits = self.book.identify(text)
        if not hits:
            return
        self._remember(event, hits[0])
        ask_detail = bool(DETAIL_ASK.search(text))
        req.system_prompt = (req.system_prompt or "") + "\n\n" + render_context(hits, ask_detail)
        logger.info(f"[{NAME}] 命中 {len(hits)} 条：{[h['name'] for h in hits]}")

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="search_xsg_meme")
    async def search_xsg_meme(self, event: AstrMessageEvent, query: str):
        '''查《新三国》(2010版) 和折棒吐槽系列的梗。三层出处：原剧台词、折棒总结的机制、折棒评论区梗。

        用户提到三国相关梗但你不确定出处时先查，别硬猜。

        Args:
            query(string): 要查的梗名、台词片段或关键词
        '''
        entry = self.book.lookup(query)
        if entry is None:
            return (
                f"梗库里没有「{_clean(query, 30)}」。可以按常识接一句，"
                "但别编出处；分清是原剧、折棒总结还是评论区梗——不确定就说不知道。"
            )
        self._remember(event, entry)
        return render_card(entry, detail=True)

    # ---------- 命令 ----------

    @filter.command("新三国梗", alias={"xsg", "折棒梗"})
    async def help_command(self, event: AstrMessageEvent):
        yield event.plain_result(
            "新三国梗怎么用：\n"
            "/新三 <词>　查一条（再说「详细」给完整说明）\n"
            "/新三识别 <句子>　看这句话里有几个梗\n"
            "/新三列表　按来源分层列出来\n"
            "/新三随机　随便来一个\n"
            "直接聊天也行，说了梗我接着聊。"
        )

    @filter.command("新三", alias={"新三梗"})
    async def meme_command(self, event: AstrMessageEvent, query: str = ""):
        value = (query or "").strip()
        if not value:
            text = event.get_message_str() or ""
            value = re.sub(r"^/?\s*(?:新三梗?|新三)\s*[:：]?\s*", "", text).strip()
        if not value:
            yield event.plain_result("要查哪个？发「/新三 词」。")
            return
        entry = self.book.lookup(value)
        if entry is None:
            yield event.plain_result(f"梗库里没有「{value}」。")
            return
        self._remember(event, entry)
        yield event.plain_result(render_card(entry, detail=True))

    @filter.command("新三识别")
    async def identify_command(self, event: AstrMessageEvent, text: str = ""):
        value = (text or "").strip()
        if not value:
            full = event.get_message_str() or ""
            value = re.sub(r"^/?\s*新三识别\s*[:：]?\s*", "", full).strip()
        if not value:
            yield event.plain_result("格式：/新三识别 一句话")
            return
        hits = self.book.identify(value, limit=6)
        if not hits:
            yield event.plain_result("这句里没认出已知的梗。")
            return
        self._remember(event, hits[0])
        lines = [f"认出 {len(hits)} 个："]
        for entry in hits:
            label = LAYER_LABEL.get(entry["layer"], "")
            lines.append(f"· {entry['name']}（{label}）— {entry['one_line']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("新三列表")
    async def list_command(self, event: AstrMessageEvent):
        lines = []
        for layer, title in (("original", "原剧梗"), ("zhebang", "折棒体系梗"), ("comment", "评论区梗")):
            names = [e["name"] for e in self.book.by_layer(layer)]
            if names:
                lines.append(f"【{title}·{len(names)}条】")
                lines.append("、".join(names[:20]) + ("…" if len(names) > 20 else ""))
        yield event.plain_result("\n".join(lines) or "梗库是空的。")

    @filter.command("新三随机")
    async def random_command(self, event: AstrMessageEvent):
        if not self.book.entries:
            yield event.plain_result("梗库是空的。")
            return
        entry = random.choice(self.book.entries)
        self._remember(event, entry)
        yield event.plain_result(render_card(entry, detail=False))
