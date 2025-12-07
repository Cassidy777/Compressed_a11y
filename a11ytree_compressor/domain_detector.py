# domain_detector.py
from typing import List, Dict, Any, Tuple

DEBUG_DOMAIN_SCORE = True   # 必要に応じて True/False 切り替え

def _dbg(domain: str, delta: int, score: int, reason: str, node: Dict[str, Any]) -> None:
    """
    ドメイン判定のスコアを 1 ステップずつ出力する簡易デバッグ関数。
    """
    if not DEBUG_DOMAIN_SCORE:
        return
    tag = node.get("tag")
    name = node.get("name")
    text = node.get("text")

    print(
        f"[DEBUG {domain}] +{delta:2d} -> {score:3d}  ({reason})  "
        f"tag={tag!r} name={name!r} text={text!r}"
    )
    

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

    has_file = has_edit = False
    has_image = has_layer = has_colors = False

    for n in nodes:
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or "").lower()
        text = (n.get("text") or "").lower()
        raw = n.get("raw") or ""

        x = 0
        y = 0
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

        # ウィンドウタイトルに "GNU Image Manipulation Program" があれば強めに加点
        # （ただし「必須条件」にはしない）
        if "gnu image manipulation program" in name or "gnu image manipulation program" in text:
            delta = 20
            score += delta
            _dbg("gimp", delta, score, "window title 'GNU Image Manipulation Program'", n)

        # メニューバー (y ≈ 60) での判定
        if tag == "menu" and 40 <= y <= 90:
            if name == "file":
                has_file = True
                delta = 2
                score += delta
                _dbg("gimp", delta, score, "menu 'File' in top menubar", n)
            elif name == "edit":
                has_edit = True
                delta = 2
                score += delta
                _dbg("gimp", delta, score, "menu 'Edit' in top menubar", n)
            elif name == "image":
                has_image = True
                delta = 8   # GIMP 特有なので高め
                score += delta
                _dbg("gimp", delta, score, "menu 'Image' in top menubar", n)
            elif name == "layer":
                has_layer = True
                delta = 8
                score += delta
                _dbg("gimp", delta, score, "menu 'Layer' in top menubar", n)
            elif name == "colors":
                has_colors = True
                delta = 8
                score += delta
                _dbg("gimp", delta, score, "menu 'Colors' in top menubar", n)
            elif name == "filters":
                delta = 5
                score += delta
                _dbg("gimp", delta, score, "menu 'Filters' in top menubar", n)
            else:
                # 他アプリでも普通に出てくるメニュー (View / Insert / Format など) は
                # GIMP 判定には寄与させない
                # delta = 0
                pass

        # 右側ドックに見える要素 (x>1650) は、あくまで弱いシグナル
        if x > 1650:
            delta = 1
            score += delta
            _dbg("gimp", delta, score, "right-side dock element (x>1650)", n)

    # ★ ここが重要: GIMP らしいメニュー3点セット (Image/Layer/Colors) が揃ってなければ 0 点
    if not (has_image and has_layer and has_colors):
        if DEBUG_DOMAIN_SCORE:
            print(f"[DEBUG gimp] FINAL SCORE = 0 (no Image/Layer/Colors triad; raw score={score})")
        return 0

    # 3点セットが揃っている場合は、追加ボーナス
    delta = 5
    score += delta
    _dbg("gimp", delta, score, "bonus: has Image+Layer+Colors menus", {"tag": "meta"})

    # File+Edit も揃っていれば少しボーナス
    if has_file and has_edit:
        delta = 3
        score += delta
        _dbg("gimp", delta, score, "bonus: has File+Edit menus", {"tag": "meta"})

    if DEBUG_DOMAIN_SCORE:
        print(f"[DEBUG gimp] FINAL SCORE = {score}")

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

        # タイトル / ラベルに "LibreOffice Calc"
        if "libreoffice calc" in name or "libreoffice calc" in text:
            has_title = True
            delta = 30   # ここを少し強めにする
            score += delta
            _dbg("libreoffice_calc", delta, score, "window title or label 'LibreOffice Calc'", n)

        # メニューバーで Calc っぽいメニュー
        if tag == "menu" and 40 <= y <= 90:
            if name == "sheet":
                has_sheet_menu = True
                delta = 6
                score += delta
                _dbg("libreoffice_calc", delta, score, "menu 'Sheet' in top menubar", n)
            elif name == "data":
                has_data_menu = True
                delta = 5
                score += delta
                _dbg("libreoffice_calc", delta, score, "menu 'Data' in top menubar", n)

        # セル
        if tag == "table-cell":
            table_cell_count += 1
            # 最初の 200 個くらいまでは 1 点ずつ加点
            if table_cell_count <= 200:
                delta = 1
                score += delta
                _dbg("libreoffice_calc", delta, score, f"table-cell #{table_cell_count}", n)

    # セル数に応じたボーナス
    if table_cell_count > 50:
        delta = 10
        score += delta
        if DEBUG_DOMAIN_SCORE:
            print(f"[DEBUG libreoffice_calc] +{delta} -> {score} (bonus: table_cell_count > 50)")
    if table_cell_count > 200:
        delta = 10
        score += delta
        if DEBUG_DOMAIN_SCORE:
            print(f"[DEBUG libreoffice_calc] +{delta} -> {score} (bonus: table_cell_count > 200)")

    # Calc らしい構成が揃っていればさらにボーナス
    if has_title and has_sheet_menu and table_cell_count > 20:
        delta = 10
        score += delta
        if DEBUG_DOMAIN_SCORE:
            print(f"[DEBUG libreoffice_calc] +{delta} -> {score} (bonus: title+sheet_menu+cells)")

    if DEBUG_DOMAIN_SCORE:
        print(f"[DEBUG libreoffice_calc] FINAL SCORE = {score} (table_cell_count={table_cell_count})")

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

    if DEBUG_DOMAIN_SCORE:
        print("[DEBUG] domain scores:", scores)

    domain, best = "generic", 0  # generic のスコアは 0 とする
    for d, s in scores.items():
        if s > best:
            domain, best = d, s
    return domain


def detect_domain_and_scores(nodes: List[Dict[str, Any]]) -> Tuple[str, Dict[str, int]]:
    """
    各ドメインのスコアも同時に返す版。
    detect_domain_from_nodes と同じロジックで domain を決めつつ、
    scores = {"gimp": ..., "chrome": ...} を一緒に返す。
    """
    scores = {
        "gimp": _score_gimp(nodes),
        "chrome": _score_chrome(nodes),
        "vsc": _score_vsc(nodes),
        "libreoffice_calc": _score_libreoffice_calc(nodes),
        "libreoffice_impress": _score_libreoffice_impress(nodes),
        "libreoffice_writer": _score_libreoffice_writer(nodes),
    }

    domain, best = "generic", 0
    for d, s in scores.items():
        if s > best:
            domain, best = d, s

    return domain, scores


def detect_domain_from_nodes(nodes: List[Dict[str, Any]]) -> str:
    """
    既存の API は互換性のため残しておく。
    新しくは detect_domain_and_scores を使うのが推奨。
    """
    domain, _scores = detect_domain_and_scores(nodes)
    return domain