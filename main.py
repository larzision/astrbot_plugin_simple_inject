"""
精简版角色状态注入插件

功能：
  - 根据时段自动标注【凌晨/清晨/上午/中午/下午/傍晚/晚上】
  - 持久化好感度、情绪值，自动生成状态描述
  - 根据用户ID判断关系标签（哥哥/朋友/邻居/敌人/普通）
  - 结巴概率注入
  - 上下文管理（记录对话、时间戳、去重标记、对话摘要）
  - Bot内心想法摘要记录
  - 所有注入挂在 req.prompt 头部，不干扰历史消息缓存
"""

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api import logger


STATE_DIR = Path("data/plugin_data/simple_inject")


# ── 工具函数 ──

def _ensure_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _state_path(umo: str) -> Path:
    safe = umo.replace(":", "_").replace("/", "_").replace("\\", "_")
    return STATE_DIR / f"{safe}.json"


def _default_state(config: dict = None) -> dict:
    cfg = config or {}
    return {
        "affection": cfg.get("initial_affection", 65),
        "emotion": cfg.get("initial_emotion", 60),
        "lewdness": cfg.get("initial_lewdness", 20),
        "_last_date": "",
        "conversation_log": [],
    }


def load_state(umo: str, config: dict = None) -> dict:
    _ensure_dir()
    path = _state_path(umo)
    if not path.exists():
        state = _default_state(config)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return state
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_state(config)


def save_state(umo: str, state: dict):
    _ensure_dir()
    with open(_state_path(umo), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── 上下文工具 ──

def minutes_since_last(state: dict) -> str:
    log = state.get("conversation_log", [])
    if len(log) < 2:
        return "刚刚"
    last = log[-2]
    elapsed = int(time.time()) - last.get("time", 0)
    if elapsed < 60:
        return "刚刚"
    mins = elapsed // 60
    if mins < 60:
        return f"{mins}分钟前"
    hours = mins // 60
    return f"{hours}小时前"


def todays_duplicate_count(state: dict, user_msg: str) -> int:
    log = state.get("conversation_log", [])
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    for entry in log:
        if entry.get("role") != "user":
            continue
        if not entry.get("time"):
            continue
        entry_date = datetime.fromtimestamp(entry["time"]).strftime("%Y-%m-%d")
        if entry_date != today:
            continue
        content = entry.get("content", "")
        if content == user_msg.strip():
            count += 1
    return count


def _fmt_time_ago(ts: int) -> str:
    if not ts:
        return "?"
    elapsed = int(time.time()) - ts
    if elapsed < 60:
        return "刚刚"
    m = elapsed // 60
    if m < 60:
        return f"{m}分钟前"
    h = m // 60
    return f"{h}小时前"


def groom_history(state: dict, max_count: int, timeout_secs: int):
    if max_count <= 0:
        return
    log = state.get("conversation_log", [])
    if not log:
        return
    now = int(time.time())
    cutoff = now - timeout_secs
    recent = [e for e in log if e["time"] >= cutoff]
    expired = [e for e in log if e["time"] < cutoff]
    keep = list(recent)
    budget = max(0, max_count - len(recent))
    if expired and budget > 0:
        expired.sort(key=lambda e: e["time"])
        keep = expired[-budget:] + keep
    seen = set()
    deduped = []
    for e in keep:
        sig = (e.get("role", ""), e.get("content", e.get("miko_thought", "")), e.get("time", 0))
        if sig not in seen:
            seen.add(sig)
            deduped.append(e)
    deduped.sort(key=lambda e: e["time"])
    state["conversation_log"] = deduped


# ── 内心想法摘要 ──

def _miko_thought_summary(reply_text: str) -> str:
    if not reply_text:
        return "miko没说话"
    t = reply_text.strip()
    tech_kw = ["报错", "错误", "bug", "修复", "代码", "配置", "日志",
               "插件", "框架", "语法", "文件", "重启", "覆盖", "备份"]
    chat_kw = ["哈哈", "笑死", "可爱", "喜欢", "早啊", "晚安", "天气",
               "嗯嗯", "好哦", "行吧", "emm", "诶"]
    close_kw = ["抱抱", "贴贴", "想你了", "亲", "爱你", "摸摸"]
    lewd_kw = ["舒服", "想要", "身体", "舔", "插", "湿", "热", "紧"]
    if any(k in t for k in tech_kw):
        return "miko觉得又在折腾代码了"
    if any(k in t for k in close_kw):
        return "miko想亲近对方"
    if any(k in t for k in lewd_kw):
        return "miko有点发情了"
    if any(k in t for k in chat_kw):
        return "miko聊得挺开心"
    short = t[:10].replace("\n", " ")
    if len(t) > 10:
        short += "…"
    return f"miko回了句「{short}」"


def _user_intent_summary(user_msg: str) -> str:
    if not user_msg:
        return "用户发了空消息"
    m = user_msg.strip()
    tech_kw = ["报错", "错误", "bug", "修复", "改", "加", "删",
               "代码", "配置", "插件", "框架", "文件", "重启",
               "为什么", "怎么", "如何", "不行", "没触发", "没保存",
               "覆盖", "丢失", "写", "读", "改一下"]
    chat_kw = ["哈哈", "笑", "早", "晚", "在吗", "好", "嗯", "哦"]
    lewd_kw = ["色", "舒服", "想要", "舔", "摸", "身体"]
    complain_kw = ["烦", "累", "困", "无聊", "无语", "算了"]
    if any(k in m for k in tech_kw):
        if any(k in m for k in ["报错", "错误", "bug", "不行", "没触发"]):
            return "用户想让我看报错"
        if any(k in m for k in ["改", "加", "删", "改一下"]):
            return "用户想让我改代码"
        if any(k in m for k in ["为什么", "怎么", "如何"]):
            return "用户想问我技术问题"
        return "用户想讨论技术问题"
    if any(k in m for k in lewd_kw):
        return "用户想色色"
    if any(k in m for k in complain_kw):
        return "用户想吐槽"
    if any(k in m for k in chat_kw):
        return "用户想闲聊"
    short = m[:15].replace("\n", " ")
    if len(m) > 15:
        short += "…"
    return f"用户说了「{short}」"


def build_state_snapshot(state: dict) -> str:
    em = emotion_label(state["emotion"])
    aff = affection_desc(state["affection"])
    lewd = state.get("lewdness", 0)
    return f"情绪:{em}|好感:{aff}|淫乱:{lewd}"


def build_conversation_context(state: dict, max_entries: int = 10,
                                user_msg_max_chars: int = 200,
                                thought_mode: str = "内心想法") -> str:
    """构建对话上下文摘要，注入到提示词中"""
    log = state.get("conversation_log", [])
    if not log or max_entries <= 0:
        return ""
    recent = log[-max_entries:]
    lines = ["【近期对话记录】"]
    for entry in recent:
        role = entry.get("role", "?")
        ts = entry.get("time", 0)
        time_tag = _fmt_time_ago(ts) if ts else "?"
        if role == "user":
            content = entry.get("content", "")
            if content:
                short = content[:user_msg_max_chars]
                if len(content) > user_msg_max_chars:
                    short += "…"
                lines.append(f"[{time_tag}] 用户说: {short}")
        elif role == "assistant":
            if thought_mode == "内心想法":
                thought = entry.get("miko_thought", "")
                if thought:
                    lines.append(f"[{time_tag}] miko心想: {thought}")
            elif thought_mode == "简短":
                thought = entry.get("miko_thought", "")
                snapshot = entry.get("state_snapshot", "")
                if thought:
                    lines.append(f"[{time_tag}] miko: {thought} | {snapshot}" if snapshot else f"[{time_tag}] miko: {thought}")
            elif thought_mode == "具体":
                content = entry.get("content", "")
                snapshot = entry.get("state_snapshot", "")
                if content:
                    short = content[:user_msg_max_chars]
                    if len(content) > user_msg_max_chars:
                        short += "…"
                    lines.append(f"[{time_tag}] miko回复: {short} | {snapshot}" if snapshot else f"[{time_tag}] miko回复: {short}")
            else:
                content = entry.get("content", "")
                if content:
                    short = content[:user_msg_max_chars]
                    if len(content) > user_msg_max_chars:
                        short += "…"
                    lines.append(f"[{time_tag}] miko: {short}")
    if len(lines) > 1:
        return "\n".join(lines) + "\n"
    return ""


# ── 标签生成 ──

def period_label() -> str:
    h = datetime.now().hour
    if 0 <= h < 5:   return "凌晨"
    if 5 <= h < 8:   return "清晨"
    if 8 <= h < 12:  return "上午"
    if 12 <= h < 14: return "中午"
    if 14 <= h < 18: return "下午"
    if 18 <= h < 20: return "傍晚"
    return "晚上"


def emotion_label(val: int) -> str:
    if val >= 70: return "开心"
    if val >= 40: return "平静"
    if val >= 20: return "烦躁"
    return "低落"


def lewdness_label(val: int) -> str:
    if val >= 80: return "身体有点燥热，想做坏事"
    if val >= 50: return "脑子里偶尔飘过色色的念头"
    return ""


def affection_desc(val: int) -> str:
    if val >= 90: return "超喜欢的，有点依赖感"
    if val >= 80: return "挺喜欢的，想多聊聊"
    if val >= 50: return "印象还行吧"
    if val >= 30: return "就那样，一般般"
    return "不太想搭理..."


def parse_list(config: dict, key: str) -> list:
    raw = config.get(key, [])
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if str(u).strip()]
    if isinstance(raw, str) and raw.strip():
        return [u.strip() for u in raw.replace("，", ",").split(",") if u.strip()]
    return []


def relationship_label(user_id: str, config: dict) -> str:
    if user_id in parse_list(config, "brother_list"):
        return "他是你的亲哥哥，最熟悉的人，可以放下所有防备"
    if user_id in parse_list(config, "friend_list"):
        return "他是你的好朋友，可以信赖"
    if user_id in parse_list(config, "enemy_list"):
        return "他是你的敌人，逮到机会就怼他"
    return "他是普通群友，保持礼貌但有些自来熟"


def build_inject(state: dict, user_id: str, user_msg: str, config: dict) -> str:
    period = period_label()
    em = emotion_label(state["emotion"])
    lewd = lewdness_label(state["lewdness"])
    aff = affection_desc(state["affection"])
    relation = relationship_label(user_id, config)

    lewd_section = f"。{lewd}" if lewd else ""

    # 从配置或txt文件读取思考引导文本
    raw_guide = config.get("thinking_guide_text", "") or ""
    if not raw_guide:
        guide_path = Path(__file__).parent / "thinking_guide.txt"
        try:
            raw_guide = guide_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if not raw_guide:
        raw_guide = (
            "【思考过程引导（内心进行，不要输出）】\n"
            "在回复之前，先在内心过一遍：\n"
            "1. 理解：对方这句话到底想表达什么？有没有潜台词？\n"
            "2. 判断：这事我懂吗？在我的认知范围内吗？对方是不是在越线/冒犯？\n"
            "3. 回应：按我的性格、当前心情、和ta的关系，最适合怎么接？我的底线在哪？\n"
            "4. 边界：我不会的东西别装懂，直接说不知道/不懂/做不到。知道自己的局限。\n"
            "想完这些之后，再用你的风格给出最终回复。"
        )
    thinking_guide = raw_guide + "\n\n"

    stutter_line = ""
    prob = config.get("stutter_probability", 0.3)
    if random.random() < prob:
        stutter_line = "说话有点结巴。\n"

    dup_count = todays_duplicate_count(state, user_msg)
    minutes = minutes_since_last(state)
    dup_line = f"TA刚发了条跟之前一模一样的消息，今天已经第{dup_count}次了。\n" if dup_count > 1 else ""
    time_line = f"上条消息就在{minutes}发的。\n" if minutes != "刚刚" else "TA刚发完上一条。\n"

    state_line = f"时段：{period} | 情绪：{em} | 好感：{aff}{lewd_section}\n"
    if stutter_line:
        state_line = f"时段：{period} | 情绪：{em} | 好感：{aff}{lewd_section}\n{stutter_line}"

    # 对话上下文摘要
    ctx = ""
    if config.get("inject_conversation_context", False):
        ctx_entries = config.get("conversation_context_entries", 10)
        msg_max = config.get("user_msg_max_chars", 200)
        thought_mode = config.get("bot_thought_mode", "内心想法")
        ctx = build_conversation_context(state, max_entries=ctx_entries,
                                          user_msg_max_chars=msg_max,
                                          thought_mode=thought_mode)

    guide = f"{thinking_guide}" if config.get("thinking_mode", True) else ""

    # ── 各开关独立控制注入内容 ──
    parts = []
    
    state_parts = []
    period_str = f"时段：{period}"
    em_str = f"情绪：{em}"
    aff_str = f"好感：{aff}"
    lewd_str = f"{lewd}" if lewd else ""
    
    if config.get("inject_period", True):
        state_parts.append(period_str)
    if config.get("inject_emotion", True):
        state_parts.append(em_str)
    if config.get("inject_affection", True):
        state_parts.append(aff_str)
    
    state_line_extra = " | ".join(state_parts)
    lewd_section_extra = f"。{lewd_str}" if lewd_str and config.get("inject_lewdness", True) else ""
    state_block = f"【当前状态】\n{state_line_extra}{lewd_section_extra}\n" if state_parts or (lewd_str and config.get("inject_lewdness", True)) else ""
    
    relation_block = f"【关系】{relation}\n" if config.get("inject_relation", True) else ""
    time_block = f"{time_line}" if config.get("inject_time_info", True) else ""
    word_limit_line = f"【字数限制】每条回复绝对禁止超过{config.get('max_words', 25)}字。\n" if config.get("inject_word_limit", True) else ""

    return (
        f"{guide}"
        f"{dup_line}"
        f"{state_block}"
        f"{relation_block}"
        f"{time_block}"
        f"{stutter_line}"
        f"{ctx}"
        f"{word_limit_line}"
    )


# ── 插件主体 ──

@register("astrbot_plugin_simple_inject", "miko", "精简版状态注入插件", "1.1.2")
class SimpleInjectPlugin(Star):

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._log_cache = {}
        logger.info("状态注入精简版插件已加载")

    @filter.on_llm_request(priority=95)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("enabled", True):
            return
        try:
            umo = event.unified_msg_origin
            uid = event.message_obj.sender.user_id
            user_msg = (event.message_str or "").strip()

            state = load_state(umo, self.config)

            # 从内存缓存恢复日志（不写磁盘时）
            if not self.config.get("save_conversation_log", True) and umo in self._log_cache:
                state["conversation_log"] = self._log_cache[umo]

            # 每日重置
            today_str = datetime.now().strftime("%Y-%m-%d")
            if state.get("_last_date", "") != today_str:
                state["conversation_log"] = []
                state["_last_date"] = today_str

            # 记录用户消息
            summary_mode = self.config.get("user_msg_store_mode", "原文")
            max_chars = self.config.get("user_msg_max_chars", 200)
            if summary_mode == "总结":
                stored_content = _user_intent_summary(user_msg)
            else:
                stored_content = user_msg[:max_chars] if max_chars > 0 else user_msg

            state.setdefault("conversation_log", []).append({
                "role": "user",
                "user_id": uid,
                "content": stored_content,
                "time": int(time.time()),
            })

            # 修剪历史
            max_count = self.config.get("max_history_count", 30)
            timeout_secs = self.config.get("history_timeout_seconds", 600)
            groom_history(state, max_count, timeout_secs)

            # 保存当前用户消息到 state，供 on_llm_response 从磁盘读取
            state["_last_user_msg"] = user_msg

            # 保存到磁盘，确保 on_llm_response 能读到最新状态
            save_state(umo, state)

            inject = build_inject(state, uid, user_msg, self.config)

            if req.prompt:
                req.prompt = f"{inject}\n\n{req.prompt}"
            else:
                req.system_prompt = (req.system_prompt or "") + f"\n\n{inject}\n"

            event.set_extra("_si_state", state)
            event.set_extra("_si_umo", umo)

        except Exception as e:
            logger.warning(f"状态注入失败: {e}")

    @filter.on_llm_response(priority=95)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.config.get("enabled", True):
            return
        try:
            umo = event.unified_msg_origin
            state = load_state(umo, self.config)

            # Debug: mark that on_llm_response ran
            import os as _os
            _os.system('echo "on_llm_response ran at $(date)" >> /tmp/si_debug.log')

            # 记录 bot 回复
            resp_text = (resp.completion_text or "").strip()
            if resp_text and self.config.get("save_bot_state_to_history", True):
                thought_mode = self.config.get("bot_thought_mode", "内心想法")
                snapshot = build_state_snapshot(state)

                if thought_mode == "内心想法":
                    bot_thought = _miko_thought_summary(resp_text)
                    no_content = True
                elif thought_mode == "简短":
                    bot_thought = f"{_miko_thought_summary(resp_text)} | {snapshot}"
                    no_content = True
                elif thought_mode == "具体":
                    content_text = resp_text[:200]
                    bot_thought = f"回复:{content_text} | {snapshot}"
                    no_content = False
                else:
                    bot_thought = snapshot
                    no_content = False

                entry = {
                    "role": "assistant",
                    "time": int(time.time()),
                    "miko_thought": bot_thought,
                    "state_snapshot": snapshot,
                }
                if not no_content:
                    entry["content"] = resp_text[:200]

                state.setdefault("conversation_log", []).append(entry)
                max_count = self.config.get("max_history_count", 30)
                timeout_secs = self.config.get("history_timeout_seconds", 600)
                groom_history(state, max_count, timeout_secs)

            # 情绪微调 + 淫乱度增长
            user_msg = state.pop("_last_user_msg", "") or ""
            pos_kw = ["喜欢", "可爱", "厉害", "好", "夸", "棒", "爱", "贴贴", "抱抱", "想你了"]
            neg_kw = ["傻", "蠢", "滚", "烦", "讨厌", "恶心", "垃圾"]

            if any(k in user_msg for k in pos_kw):
                state["emotion"] = min(100, state["emotion"] + 8)
                state["affection"] = min(100, state["affection"] + 3)
            if any(k in user_msg for k in neg_kw):
                state["emotion"] = max(0, state["emotion"] - 10)
                state["affection"] = max(0, state["affection"] - 3)

            explicit_kw = ["嗯", "啊", "身体", "舒服", "想要", "舔", "摸", "插", "湿", "紧", "热"]
            if any(k in resp_text for k in explicit_kw) and state["emotion"] >= 40:
                state["lewdness"] = min(100, state["lewdness"] + random.randint(5, 15))
            if emotion_label(state["emotion"]) == "开心":
                state["lewdness"] = max(state["lewdness"], 30)
            if state["lewdness"] >= 100:
                state["lewdness"] = 0

            # 保存
            if not self.config.get("save_conversation_log", True):
                self._log_cache[umo] = list(state["conversation_log"])
                state_no_log = {k: v for k, v in state.items() if k != "conversation_log"}
                save_state(umo, state_no_log)
            else:
                save_state(umo, state)

        except Exception as e:
            logger.warning(f"状态更新失败: {e}")

    async def terminate(self):
        logger.info("状态注入精简版插件已卸载")
