# domain_detector.py
from typing import List, Dict, Any, Tuple


def _extract_xy_from_raw(raw: str) -> Tuple[int, int]:
    """
    raw の position 部分から (x, y) を抜く簡易ヘルパー。
    失敗したら (0, 0)。
    """
    if not raw or "\t(" not in raw:
        return 0, 0
    try:
        parts = raw.split("\t")
        if len(parts) < 6:
            return 0, 0
        pos = parts[5].strip()
        if not (pos.startswith("(") and pos.endswith(")")):
            return 0, 0
        pos = pos[1:-1]
        x_str, y_str = pos.split(",")
        return int(x_str.strip()), int(y_str.strip())
    except Exception:
        return 0, 0


def _estimate_screen_size(nodes: List[Dict[str, Any]]) -> Tuple[int, int]:
    """
    すべてのノードの (x+w, y+h) の最大値から screen_w, screen_h をざっくり推定する。
    失敗時は (1920, 1080) を返す。
    """
    max_x = 0
    max_y = 0
    for n in nodes:
        raw = n.get("raw") or ""
        x, y = _extract_xy_from_raw(raw)
        parts = raw.split("\t")
        w = h = 0
        if len(parts) >= 7:
            size = parts[6].strip()
            if size.startswith("(") and size.endswith(")"):
                try:
                    w_str, h_str = size[1:-1].split(",")
                    w = int(w_str.strip())
                    h = int(h_str.strip())
                except Exception:
                    pass
        max_x = max(max_x, x + w)
        max_y = max(max_y, y + h)
    if max_x <= 0:
        max_x = 1920
    if max_y <= 0:
        max_y = 1080
    return max_x, max_y



def _score_chrome(nodes: List[Dict[str, Any]]) -> int:
    score = 0
    for n in nodes:
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or "").lower()
        text = (n.get("text") or "").lower()
        
        # Chrome ウィンドウタイトル
        if "google chrome" in name:
            score += 15

        # アドレスバー (決定打)
        if tag == "entry" and "address and search bar" in name:
            score += 20

        # Chromeの上の方のボタン
        if tag == "push-button" and name in (
            "search tabs",
            "new tab",
            "bookmark this tab",
            "side panel",
            "you",
            "new chrome available",
            "google apps",
        ):
            score += 6

        # ブックマークダイアログでよく出るやつ
        if tag in ("entry", "push-button") and name in (
            "bookmark name",
            "folder",
            "done",
        ):
            score += 4

        # Chromeのホームでよく出る上部リンク
        if tag == "link" and name in ("gmail", "search for images"):
            score += 3

        # link が多いときも少し足しておく
        if tag == "link":
            score += 1

    return score


def _score_gimp(nodes: List[Dict[str, Any]]) -> int:
    score = 0
    has_file = has_edit = has_image = has_layer = has_colors = False

    for n in nodes:
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or "").lower()
        text = (n.get("text") or "").lower()
        raw = n.get("raw") or ""
        y = 0
        x = 0

        # ウィンドウタイトル判定
        if "gnu image manipulation program" in name:
            score += 20

        if "\t(" in raw:
            try:
                parts = raw.split("\t")
                if len(parts) >= 6:
                    pos = parts[5].strip("()")
                    x_str, y_str = pos.split(",")
                    x = int(x_str.strip())
                    y = int(y_str.strip())
            except Exception:
                pass

        # GIMPでほぼ必ず出るメニュー列（yが60前後）
        if tag == "menu" and 40 <= y <= 90:
            if name == "file":
                has_file = True
                score += 3
            elif name == "edit":
                has_edit = True
                score += 2
            elif name == "image":
                has_image = True
                score += 4   # GIMP特有なので高め
            elif name == "layer":
                has_layer = True
                score += 4
            elif name == "colors":
                has_colors = True
                score += 4
            elif name == "filters": # 追加
                score += 4
            else:
                score += 1

        # GIMP特有のダイアログ
        if "gimp built-in srgb" in name or "gimp built-in srgb" in text:
            score += 6
        if "convert the image to the built-in srgb" in text:
            score += 5

        # 右ドックっぽいものが右端にある
        if x > 1650:
            score += 1

    # ★★★ ここがポイント ★★★
    # GIMP特有の Image / Layer / Colors のメニューが揃っていなければ 0 点扱いにする
    if not (has_image and has_layer and has_colors):
        # ただし、ダイアログだけ出ているケースもあるので、スコアが十分高ければ残す
        if score < 10:
            return 0

    # ちゃんとGIMPらしければボーナス
    if has_file and has_edit:
        score += 5

    return score


def _score_vsc(nodes: List[Dict[str, Any]]) -> int:
    score = 0
    for n in nodes:
        name = (n.get("name") or "").lower()
        text = (n.get("text") or "").lower()
        if "visual studio code" in name or "visual studio code" in text:
            score += 20 # 決定打
    return score


def _score_libreoffice_calc(nodes: List[Dict[str, Any]]) -> int:
    score = 0
    has_title = False
    has_sheet_menu = False
    has_data_menu = False
    has_function_wizard = False
    has_pivot_table = False
    has_freeze = False
    table_cell_count = 0

    for n in nodes:
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or "").lower()
        text = (n.get("text") or "").lower()
        raw = n.get("raw") or ""
        y = 0

        if "\t(" in raw:
            try:
                parts = raw.split("\t")
                if len(parts) >= 6:
                    pos = parts[5].strip("()")
                    _, y_str = pos.split(",")
                    y = int(y_str.strip())
            except Exception:
                pass

        # ウィンドウタイトルやメニューバーに LibreOffice Calc
        if "libreoffice calc" in name or "libreoffice calc" in text:
            has_title = True
            score += 20

        # メニュー列（Calc 特有の Sheet / Data など）
        if tag == "menu" and 40 <= y <= 90:
            if name == "sheet":
                has_sheet_menu = True
                score += 5
            elif name == "data":
                has_data_menu = True
                score += 4

        # ツールバーの Calc 特有ボタン
        if tag == "push-button":
            if name == "function wizard":
                has_function_wizard = True
                score += 4
            elif name == "pivot table":
                has_pivot_table = True
                score += 4
            elif name == "freeze rows and columns":
                has_freeze = True
                score += 4
            elif name in ("chart", "autoFilter", "row", "column"):
                score += 2

        # セル
        if tag == "table-cell":
            table_cell_count += 1
            if table_cell_count <= 50:
                score += 1  # ある程度まで線形に加点

    # セルがたくさんあればさらにボーナス
    if table_cell_count > 50:
        score += 10
    if table_cell_count > 200:
        score += 10

    # Calc らしい構成が揃っていればさらに加点
    if has_title and has_sheet_menu and table_cell_count > 20:
        score += 10

    return score


def _score_libreoffice_impress(nodes: List[Dict[str, Any]]) -> int:
    score = 0
    has_slide_menu = False
    has_slideshow_menu = False

    for n in nodes:
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or "").lower()
        text = (n.get("text") or "").lower()

        if "libreoffice impress" in name or "libreoffice presentation" in name:
            score += 20

        if tag == "menu":
            if name == "slide":
                has_slide_menu = True
                score += 5
            elif name == "slide show":
                has_slideshow_menu = True
                score += 5

        if tag == "document-presentation":
            score += 15

    if has_slide_menu and has_slideshow_menu:
        score += 5

    return score

def _score_libreoffice_writer(nodes: List[Dict[str, Any]]) -> int:
    """LibreOffice Writer (Word) のスコアリング"""
    score = 0
    has_styles_menu = False
    has_table_menu = False # CalcにはなくWriterにあるTableメニュー

    for n in nodes:
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or "").lower()
        text = (n.get("text") or "").lower()
        
        if "libreoffice writer" in name:
            score += 20
        
        if tag == "menu":
            if name == "styles":
                has_styles_menu = True
                score += 5
            elif name == "table":
                # Calc の Table と混同しないよう注意だが、Writer の Table メニューは特徴的
                has_table_menu = True
                score += 3
        
        if tag == "document-text": # Writerの本文エリア
            score += 15

    if has_styles_menu and has_table_menu:
        score += 5

    return score


def detect_domain_from_nodes(nodes: List[Dict[str, Any]]) -> str:
    # 全ドメインのスコアを計算して、最も高いものを返す
    scores = {
        "gimp": _score_gimp(nodes),
        "chrome": _score_chrome(nodes),
        "vsc": _score_vsc(nodes),
        "libreoffice_calc": _score_libreoffice_calc(nodes), 
        "libreoffice_impress": _score_libreoffice_impress(nodes),
        "libreoffice_writer": _score_libreoffice_writer(nodes),
    }

    # デバッグ用にスコアを見たい場合はここで print(scores) してもよい
    
    domain, best = "generic", 0 # generic のスコアは 0 とする
    for d, s in scores.items():
        if s > best:
            domain, best = d, s

    return domain