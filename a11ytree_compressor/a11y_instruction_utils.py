# a11y_instruction_utils.py
import re
from typing import Set

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