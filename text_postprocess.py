"""
识别结果后处理：简体化、词表纠错、配置替换、TTS 回声过滤。

流水线见 postprocess_transcript()；配置见 docs/CORE_API.md#text_postprocesspy--识别后处理
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Common chars that appear in Whisper output as traditional but rarely in mainland UI text.
# Fallback when zhconv is not installed (offline bundle without extra pip).
_BUILTIN_T2S: Dict[str, str] = {
    "國": "国", "語": "语", "說": "说", "對": "对", "體": "体", "這": "这",
    "們": "们", "時": "时", "開": "开", "門": "门", "聽": "听", "見": "见",
    "現": "现", "發": "发", "網": "网", "絡": "络", "電": "电", "腦": "脑",
    "機": "机", "構": "构", "設": "设", "備": "备", "報": "报", "警": "警",
    "樓": "楼", "層": "层", "間": "间", "號": "号", "碼": "码", "認": "认",
    "識": "识", "調": "调", "試": "试", "運": "运", "行": "行", "關": "关",
    "閉": "闭", "啟": "启", "動": "动", "聲": "声", "響": "响", "麥": "麦",
    "傳": "传", "輸": "输", "據": "据", "數": "数", "質": "质", "問": "问",
    "題": "题", "環": "环", "檢": "检", "測": "测", "連": "连", "接": "接",
    "斷": "断", "開": "开", "錄": "录", "音": "音", "頻": "频", "視": "视",
    "監": "监", "護": "护", "醫": "医", "療": "疗", "藥": "药", "劑": "剂",
    "溫": "温", "濕": "湿", "壓": "压", "氣": "气", "煙": "烟", "霧": "雾",
    "應": "应", "詢": "询", "確": "确", "認": "认", "執": "执", "結": "结",
    "顯": "显", "示": "示", "選": "选", "擇": "择", "項": "项", "標": "标",
    "籤": "签", "類": "类", "型": "型", "狀": "状", "態": "态", "啟": "启",
    "禁": "禁", "用": "用", "復": "复", "位": "位", "清": "清", "除": "除",
    "歷": "历", "史": "史", "記": "记", "錄": "录", "誌": "志", "錯": "错",
    "誤": "误", "幫": "帮", "助": "助", "說": "说", "明": "明", "檔": "档",
    "案": "案", "載": "载", "導": "导", "出": "出", "入": "入", "備": "备",
    "還": "还", "原": "原", "級": "级", "版": "版", "權": "权", "註": "注",
    "冊": "册", "錄": "录", "賬": "账", "戶": "户", "碼": "码", "組": "组",
    "織": "织", "員": "员", "戶": "户", "訂": "订", "單": "单", "產": "产",
    "價": "价", "庫": "库", "倉": "仓", "務": "务", "財": "财", "發": "发",
    "票": "票", "結": "结", "算": "算", "審": "审", "批": "批", "務": "务",
    "務": "务", "郵": "邮", "話": "话", "話": "话", "話": "话", "導": "导",
    "車": "车", "輛": "辆", "貨": "货", "裝": "装", "卸": "卸", "稱": "称",
    "儀": "仪", "錶": "表", "維": "维", "護": "护", "養": "养", "障": "障",
    "統": "统", "計": "计", "圖": "图", "錶": "表", "勢": "势", "預": "预",
    "測": "测", "塊": "块", "協": "协", "議": "议", "關": "关", "瀏": "浏",
    "覽": "览", "頁": "页", "鈕": "钮", "單": "单", "對": "对", "話": "话",
    "輸": "输", "掃": "扫", "複": "复", "製": "制", "傳": "传", "視": "视",
    "樂": "乐", "暫": "暂", "進": "进", "退": "退", "靜": "静", "縮": "缩",
    "評": "评", "論": "论", "點": "点", "贊": "赞", "轉": "转", "發": "发",
    "關": "关", "注": "注", "絲": "丝", "風": "风", "膚": "肤", "語": "语",
    "譯": "译", "線": "线", "異": "异", "進": "进", "內": "内", "緩": "缓",
    "隊": "队", "棧": "栈", "針": "针", "組": "组", "異": "异", "斷": "断",
    "監": "监", "視": "视", "壓": "压", "縮": "缩", "簽": "签", "鑰": "钥",
    "隨": "随", "鐘": "钟", "區": "区", "臺": "台", "灣": "湾", "與": "与",
    "為": "为", "無": "无", "從": "从", "來": "来", "個": "个", "會": "会",
    "業": "业", "產": "产", "業": "业", "經": "经", "濟": "济", "區": "区",
    "縣": "县", "鎮": "镇", "鄉": "乡", "廠": "厂", "礦": "矿", "農": "农",
    "業": "业", "漁": "渔", "獵": "猎", "獸": "兽", "鳥": "鸟", "魚": "鱼",
    "龍": "龙", "鳳": "凤", "馬": "马", "車": "车", "東": "东", "西": "西",
    "南": "南", "北": "北", "廣": "广", "場": "场", "園": "园", "館": "馆",
    "廳": "厅", "室": "室", "廚": "厨", "衛": "卫", "浴": "浴", "臥": "卧",
    "書": "书", "畫": "画", "筆": "笔", "紙": "纸", "墨": "墨", "顏": "颜",
    "紅": "红", "綠": "绿", "藍": "蓝", "黃": "黄", "銀": "银", "銅": "铜",
    "鐵": "铁", "鋼": "钢", "鋁": "铝", "錫": "锡", "鉛": "铅", "鋅": "锌",
    "鎳": "镍", "鈦": "钛", "鎂": "镁", "鈉": "钠", "鉀": "钾", "鈣": "钙",
    "裡": "里", "裏": "里", "後": "后", "裡": "里", "麼": "么", "於": "于",
    "並": "并", "幹": "干", "纔": "才", "纔": "才", "纔": "才", "纔": "才",
    "臺": "台", "臺": "台", "纔": "才", "纔": "才", "纔": "才",
}

_DEFAULT_SUPPRESS = ["我在", "在呢", "我在呢", "嗯", "啊", "好的", "好"]


def normalize_compare(s: str) -> str:
    """去掉空白与常见标点，用于回声/短语比对。"""
    if not s:
        return ""
    return re.sub(
        r"[\s\u3000，。！？、；：\"“”''（）\[\].,!?;:'·]+",
        "",
        s.strip(),
    )


def _domain_keywords_from_config(config: dict) -> List[str]:
    """Only explicit domain_keywords — never split initial_prompt/style prompt into replacements."""
    w = config.get("whisper") or {}
    kws = w.get("domain_keywords")
    out: List[str] = []
    if isinstance(kws, list):
        out.extend(str(k).strip() for k in kws if k and str(k).strip())
    elif isinstance(kws, str) and kws.strip():
        out.extend(kws.split())
    # dedupe, longest first for correction pass
    seen: set = set()
    unique: List[str] = []
    for k in sorted(out, key=len, reverse=True):
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def get_postprocess_config(config: dict) -> Dict[str, Any]:
    """
    合并 postprocess 与 whisper.* 覆盖项，得到后处理用配置 dict。

    含 output_simplified、suppress_phrases、domain_keywords、replacements 等。
    """
    pp = dict(config.get("postprocess") or {})
    w = config.get("whisper") or {}
    domain_keywords = _domain_keywords_from_config(config)
    return {
        "output_simplified": pp.get(
            "output_simplified", w.get("output_simplified", True)
        ),
        "post_wake_grace_ms": int(
            pp.get("post_wake_grace_ms", w.get("post_wake_grace_ms", 1500))
        ),
        "suppress_phrases": list(
            pp.get("suppress_phrases", w.get("suppress_phrases", _DEFAULT_SUPPRESS))
        ),
        "domain_keywords": domain_keywords,
        "domain_keyword_correct": bool(
            pp.get(
                "domain_keyword_correct",
                w.get("domain_keyword_correct", True),
            )
        ),
        "keyword_correct_mode": str(
            pp.get(
                "keyword_correct_mode",
                w.get("keyword_correct_mode", "homophone"),
            )
        ).strip().lower()
        or "homophone",
        "replacements": _parse_replacements(
            pp.get("replacements", w.get("replacements"))
        ),
    }


def _parse_replacements(raw: Any) -> List[Tuple[str, str]]:
    """
    Normalize replacement rules from config.

    Supported shapes:
      - {"错词": "正词", ...}
      - [{"from": "错词", "to": "正词"}, ...]
      - [["错词", "正词"], ...]
      - ["错词=正词", "错词2=正词2"]
    """
    pairs: List[Tuple[str, str]] = []
    if not raw:
        return pairs
    if isinstance(raw, dict):
        for src, dst in raw.items():
            s, d = str(src).strip(), str(dst).strip()
            if s and d:
                pairs.append((s, d))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                s = str(item.get("from", item.get("src", ""))).strip()
                d = str(item.get("to", item.get("dst", ""))).strip()
                if s and d:
                    pairs.append((s, d))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                s, d = str(item[0]).strip(), str(item[1]).strip()
                if s and d:
                    pairs.append((s, d))
            elif isinstance(item, str) and item.strip():
                line = item.strip()
                if "=" in line:
                    s, _, d = line.partition("=")
                    s, d = s.strip(), d.strip()
                    if s and d:
                        pairs.append((s, d))
    # Longer source first so e.g. "消控室" wins over "消控"
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    seen: set = set()
    out: List[Tuple[str, str]] = []
    for s, d in pairs:
        if s not in seen:
            seen.add(s)
            out.append((s, d))
    return out


def apply_configured_replacements(
    text: str, pairs: Sequence[Tuple[str, str]]
) -> str:
    """
    按配置将识别文本中的源短语替换为目标短语（全局 replace）。

    pairs 应按源串长度降序排列（get_postprocess_config 已处理）。
    """
    if not text or not pairs:
        return text
    out = text
    for src, dst in pairs:
        if src not in out:
            continue
        new = out.replace(src, dst)
        if new != out:
            logger.debug("Transcript replace %r -> %r", src, dst)
            out = new
    return out


def _toneless_pinyin_list(text: str) -> Optional[List[str]]:
    try:
        from pypinyin import Style, lazy_pinyin  # type: ignore

        return lazy_pinyin(text, style=Style.NORMAL, errors="ignore")
    except ImportError:
        return None


def _syllable_close(a: str, b: str) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x == y for x, y in zip(a, b)) >= len(a) - 1

    def _one_edit_away(short: str, long: str) -> bool:
        if len(long) != len(short) + 1:
            return False
        for i in range(len(long)):
            if short == long[:i] + long[i + 1 :]:
                return True
        return False

    if len(a) < len(b):
        return _one_edit_away(a, b)
    return _one_edit_away(b, a)


def _homophone_match(chunk: str, keyword: str) -> bool:
    """
    Same length; toneless pinyin same or one-syllable near-miss (紧情→警情).
    For 2-char terms, also allow same last character + near first syllable
    (志务→勤务) without rewriting unrelated pairs (火警≠警情).
    """
    if chunk == keyword or len(chunk) != len(keyword) or len(keyword) < 2:
        return False
    if len(keyword) == 2 and chunk[1] == keyword[1]:
        if sum(a != b for a, b in zip(chunk, keyword)) == 1:
            return True
    pc = _toneless_pinyin_list(chunk)
    pk = _toneless_pinyin_list(keyword)
    if pc is None or pk is None or len(pc) != len(pk):
        return False
    if len(keyword) == 2 and chunk[1] == keyword[1]:
        if pc[0] == pk[0]:
            return True
        if _syllable_close(pc[0], pk[0]):
            return True
        return False
    for a, b in zip(pc, pk):
        if a == b:
            continue
        if not _syllable_close(a, b):
            return False
    return True


def _shape_match(chunk: str, keyword: str) -> bool:
    """Legacy same-length glyph similarity (looser; use keyword_correct_mode=shape)."""
    if chunk == keyword or len(chunk) != len(keyword) or len(keyword) < 2:
        return False
    same = sum(a == b for a, b in zip(chunk, keyword))
    n = len(keyword)
    if n == 2:
        return same >= 2
    if n <= 4:
        return same / n >= 0.75
    return same / n >= 0.8


def _should_replace_chunk(chunk: str, keyword: str, mode: str) -> bool:
    if chunk == keyword:
        return False
    if mode in ("off", "false", "0", "none"):
        return False
    if mode in ("shape", "glyph", "legacy"):
        return _shape_match(chunk, keyword)
    return _homophone_match(chunk, keyword)


def apply_domain_keyword_corrections(
    text: str,
    keywords: Sequence[str],
    *,
    mode: str = "homophone",
) -> str:
    """
    用 domain_keywords 对 ASR 误听做同音/近音纠错（默认 homophone 模式）。

    mode=shape 时使用字形相似度（较松）；mode=off 时跳过。
    """
    if not text or not keywords or mode in ("off", "false", "0", "none"):
        return text
    kws = [k for k in keywords if k and len(k) >= 2]
    if not kws:
        return text
    kws.sort(key=len, reverse=True)
    out = text
    for kw in kws:
        n = len(kw)
        i = 0
        while i <= len(out) - n:
            chunk = out[i : i + n]
            if chunk == kw:
                i += n
                continue
            if _should_replace_chunk(chunk, kw, mode):
                out = out[:i] + kw + out[i + n :]
                i += n
            else:
                i += 1
    return out


def to_simplified_chinese(text: str) -> str:
    """繁体转简体：优先 zhconv，离线包无 zhconv 时用内置字表。"""
    if not text:
        return text
    try:
        import zhconv  # type: ignore

        return zhconv.convert(text, "zh-cn")
    except ImportError:
        pass
    return "".join(_BUILTIN_T2S.get(c, c) for c in text)


def should_suppress_transcript(text: str, phrases: List[str]) -> bool:
    """
    判断是否应丢弃整句（TTS 回声 / 唤醒反馈，如「我在」「嗯」）。

    归一化后与 suppress_phrases 完全相等，或短语为主且仅多 ≤2 字符时丢弃。
    """
    t = normalize_compare(text)
    if not t:
        return True
    for raw in phrases:
        p = normalize_compare(raw)
        if not p:
            continue
        if t == p:
            return True
        # Short echo: transcript is only the phrase plus ≤2 extra chars (punctuation etc.)
        if p in t and len(t) <= len(p) + 2:
            return True
    return False


def postprocess_transcript(
    text: str,
    cfg: Dict[str, Any],
    *,
    force_simplified: Optional[bool] = None,
) -> str:
    """
    识别结果后处理主入口：简体化 → 词表纠错 → replacements → 回声过滤。

    若应丢弃（空或命中 suppress_phrases）返回空串。
    """
    if not text or not str(text).strip():
        return ""
    out = str(text).strip()
    use_simp = (
        force_simplified
        if force_simplified is not None
        else bool(cfg.get("output_simplified", True))
    )
    if use_simp:
        out = to_simplified_chinese(out)
    if cfg.get("domain_keyword_correct", True):
        kws = cfg.get("domain_keywords") or []
        mode = str(cfg.get("keyword_correct_mode", "homophone"))
        if kws:
            corrected = apply_domain_keyword_corrections(out, kws, mode=mode)
            if corrected != out:
                out = corrected
    repl = cfg.get("replacements") or []
    if repl:
        replaced = apply_configured_replacements(out, repl)
        if replaced != out:
            out = replaced
    phrases = cfg.get("suppress_phrases") or _DEFAULT_SUPPRESS
    if should_suppress_transcript(out, phrases):
        return ""
    return out
