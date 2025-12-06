# a11y_instruction_utils.py
import re
from typing import List, Dict, Set, Tuple, Optional

# GUI操作において「意味の薄い」単語リスト
STOP_WORDS: Set[str] = {
    # English standard
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "is", "are", "am", "be", "this", "that", "it",
    # Task specific
    "please", "can", "could", "would", "you", "i", "my", "me",
    "need", "want", "try", "make", "let",
    # UI actions/nouns (指示によく出るが、対象物の特定には役に立たない語)
    "click", "tap", "press", "hit", "select", "choose",
    "open", "go", "browse", "navigate", "find", "search", "check", "uncheck",
    "button", "link", "tab", "menu", "window", "page", "website", "site",
    "input", "enter", "type", "fill", "text", "box", "field"
}


def get_instruction_keywords(instruction: str) -> Set[str]:
    """
    Instruction から検索用キーワードを抽出する。
    日本語も消えないように \w (Unicode word char) を使用。
    """
    if not instruction:
        return set()

    # 小文字化
    text = instruction.lower()
    
    # 記号を除去 (日本語や英数字は \w で残る)
    # [^\w\s] = 文字(Alphanumeric+Kanji/Kana)と空白 以外を削除
    clean_text = re.sub(r'[^\w\s]', ' ', text)
    
    words = set(clean_text.split())
    
    # ストップワード除去 & 1文字だけのゴミ除去（漢字1文字は意味がある場合が多いので英語のみ2文字以下を除去する手もあるが、一旦簡易に len>1 とする）
    keywords = {w for w in words if w not in STOP_WORDS and len(w) > 1}
    
    return keywords


def smart_truncate(
    text: str,
    keywords: Set[str],
    max_len: int = 140,
    window: int = 70,
) -> str:
    """
    キーワードが含まれる場合はその周辺を切り出して返す (Context Window)。
    含まれない場合は先頭 max_len 文字＋"..." の通常圧縮。
    """
    if not text:
        return ""

    text_lower = text.lower()

    matched_keyword = None
    match_index = -1

    # 一番最初に見つかったキーワードを採用
    for kw in keywords:
        idx = text_lower.find(kw)
        if idx != -1:
            matched_keyword = kw
            match_index = idx
            break

    # --- ヒットした場合: キーワード周辺を切り出す ---
    if match_index != -1:
        # ウィンドウの計算
        start = max(0, match_index - window)
        # end は キーワード長さを考慮
        kw_len = len(matched_keyword)
        end = min(len(text), match_index + kw_len + window)

        snippet = text[start:end]

        # 文頭・文末が切れているなら "..." を付与
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(text) else ""

        # 改行や連続空白を正規化
        snippet = re.sub(r"\s+", " ", snippet).strip()
        return f"{prefix}{snippet}{suffix}"

    # --- ヒットしない場合: 通常の truncate ---
    clean_text = re.sub(r"\s+", " ", text).strip()
    if len(clean_text) > max_len:
        return clean_text[:max_len] + "..."
    
    return clean_text


# ============================================================
# LibreOffice Calc 向けヘルパー
# ============================================================
_CELL_REF_RE = re.compile(r"\b([A-Z]{1,3}[0-9]{1,7})\b")
_RANGE_RE = re.compile(
    r"\b([A-Z]{1,3}[0-9]{1,7})\s*[:\-]\s*([A-Z]{1,3}[0-9]{1,7})\b"
)


def extract_calc_quoted_terms(instruction: str) -> List[str]:
    """
    Calc タスク向け:
    'Old ID', "Sheet2" など、クォートで囲まれたフレーズをすべて取得する。
    例:
      "copy 'Old ID' to 'New 7 Digit Id'" -> ["Old ID", "New 7 Digit Id"]
    """
    if not instruction:
        return []
    # '...' と "..." の両方を対象にする
    single = re.findall(r"'([^']+)'", instruction)
    double = re.findall(r'"([^"]+)"', instruction)
    return single + double


def extract_calc_cell_ranges(instruction: str) -> List[Tuple[str, str]]:
    """
    Calc タスク向け:
    "B1:E30", "A1:B1", "B2:F5" などのセル範囲を (start, end) で返す。
    """
    if not instruction:
        return []
    return _RANGE_RE.findall(instruction)


def extract_calc_cell_refs(instruction: str) -> List[str]:
    """
    Calc タスク向け:
    単一セル参照 "B1", "C5" などをすべて拾う。
    範囲の一部としても出現するが、重複はそのまま返す。
    """
    if not instruction:
        return []
    return _CELL_REF_RE.findall(instruction)


def extract_calc_column_hints(instruction: str) -> Dict[str, Set[str]]:
    """
    Calc タスク向け:
    Instruction から「列」を指していそうなヒントを抽出する。

    例:
      - "the 'Old ID' column"
      - "in the Gross profit column"
      - "column A"
      - "columns B to E"

    戻り値:
      {
        "header_terms": {"old id", "new 7 digit id", "gross profit", ...},
        "letters": {"A", "B", "C"},
      }
    """
    header_terms: Set[str] = set()
    letters: Set[str] = set()

    if not instruction:
        return {"header_terms": header_terms, "letters": letters}

    text = instruction

    # 1) 'Old ID' column / column 'Old ID' / column named 'Old ID'
    #    → クォート内＋"column" 付近のものだけ header 候補にする
    for m in re.finditer(
        r"(?:column|columns)\s+(?:named\s+)?'([^']+)'", text, flags=re.IGNORECASE
    ):
        header_terms.add(m.group(1).strip().lower())

    for m in re.finditer(
        r"'([^']+)'\s+(?:column|columns)", text, flags=re.IGNORECASE
    ):
        header_terms.add(m.group(1).strip().lower())

    # 2) "the Gross profit column" のようにクォートなしで出るパターン
    for m in re.finditer(
        r"(?:the\s+)?([A-Za-z0-9 _]+?)\s+column", text, flags=re.IGNORECASE
    ):
        phrase = m.group(1).strip()
        # 短すぎる or 汎用的ワードは無視（"this", "that" など）
        if not phrase:
            continue
        lower = phrase.lower()
        if lower in STOP_WORDS:
            continue
        header_terms.add(lower)

    # 3) "column A", "columns B to E"
    for m in re.finditer(
        r"(?:column|columns)\s+([A-Z])\b", text, flags=re.IGNORECASE
    ):
        letters.add(m.group(1).upper())

    # "columns B to E" の B, E だけでも拾う（間の展開は呼び出し側でやっても良い）
    for m in re.finditer(
        r"(?:column|columns)\s+([A-Z])\s+(?:to|-)\s+([A-Z])",
        text,
        flags=re.IGNORECASE,
    ):
        start, end = m.group(1).upper(), m.group(2).upper()
        letters.add(start)
        letters.add(end)

    return {
        "header_terms": header_terms,
        "letters": letters,
    }


def summarize_calc_instruction(instruction: str) -> Dict[str, object]:
    """
    LibreOffice Calc 専用の「Instruction 要約」ヘルパー。
    - クォート内の用語（列名・シート名候補など）
    - セル範囲
    - セル参照
    - 列ヒント（ヘッダ名候補 & 列文字）

    例: LibreOfficeCalcCompressor から呼び出して、
        relevant columns / ranges を決める材料にする。
    """
    return {
        "quoted_terms": extract_calc_quoted_terms(instruction),
        "cell_ranges": extract_calc_cell_ranges(instruction),
        "cell_refs": extract_calc_cell_refs(instruction),
        "column_hints": extract_calc_column_hints(instruction),
    }