import math
from typing import List, Dict, Any, Tuple, Optional, Set
from .common_ops import node_bbox_from_raw, bbox_to_center_tuple, Node, clean_modal_nodes

# ============================================================================
# 1. Scoring Helpers (Diff検出用のスコアリング)
# ============================================================================

HIGH_CONFIDENCE_ROLES = {
    "dialog", "alertdialog", "menu", "menuitem",
    "listbox", "combobox", "window", "tooltip", "popover",
}

LOW_CONFIDENCE_ROLES = {
    "label", "paragraph", "static", "image", "heading",
    "link", "section", "generic",
}

# role が空でも tag がこれなら「そこそこインタラクティブ」とみなす
INTERACTIVE_TAGS = {
    "push-button", "menu-item", "combo-box", "list",
    "list-item", "entry", "check-box", "radio-button",
}


def calculate_modal_score(
    diff_nodes: List[Node],
    last_action_point: Optional[Tuple[int, int]] = None,
) -> float:
    """
    差分ノード群が「本当にモーダルか？」を判定するスコアを計算する。
    """
    score = 0.0
    has_strong_role = False

    # ★修正1: 強いボタンのキーワード定義を追加（成功させるための重要な加点要素）
    STRONG_BUTTONS = {
        "ok", "cancel", "close", "save", "apply",
        "yes", "no", "agree", "accept", "decline",
        "delete", "remove", "done", "search", "switch"
    }

    # --- 1. Role / Tag によるスコア ---
    for node in diff_nodes:
        role = (node.get("role") or "").lower()
        tag  = (node.get("tag") or "").lower()

        if role in HIGH_CONFIDENCE_ROLES:
            score += 2.0
            has_strong_role = True
        elif role in LOW_CONFIDENCE_ROLES:
            score -= 0.5

        # role 無しでも、インタラクティブな tag なら少し加点
        if not role and tag in INTERACTIVE_TAGS:
            score += 0.7

        # 代表的なボタンラベルは追加で強くする
        label = (node.get("name") or node.get("text") or "").strip()
        lower = label.lower()
        
        # ★修正1の適用: キーワードが含まれていれば加点
        if tag == "push-button":
            # "Close" や "Yes, switch..." などの重要ボタンを拾う
            if any(kw in lower for kw in STRONG_BUTTONS):
                score += 1.5
                has_strong_role = True
                
        # 入力欄があるなら、それは対話的UIとみなす！
        if tag in INPUT_TAGS:
            score += 1.5
            has_strong_role = True  

    # 強い role / 強いボタンが 1 つも無いなら大きく減点
    if not has_strong_role:
        score -= 5.0

    # --- 2. 差分ノードの数 ---
    n_diff = len(diff_nodes)
    if n_diff < 3 and not has_strong_role:
        score -= 3.0
    elif n_diff >= 6:
        score += 1.0

    # --- 3. 直前クリックとの距離 ---
    if last_action_point and diff_nodes:
        xs = []
        ys = []
        for n in diff_nodes:
            try:
                bbox = node_bbox_from_raw(n)
                cx, cy = bbox_to_center_tuple(bbox)
                xs.append(cx)
                ys.append(cy)
            except:
                pass
        
        if xs and ys:
            center_x = sum(xs) / len(xs)
            center_y = sum(ys) / len(ys)
            dist = math.hypot(center_x - last_action_point[0],
                              center_y - last_action_point[1])

            if dist < 300:
                score += 3.0
            elif dist > 900:
                score -= 1.0

    return score

# ============================================================
# 「同じノードか？」の判定（グローバル関数）
# ============================================================

def same_node(a: Node, b: Node, tol_x: float = 25.0, tol_y: float = 25.0) -> bool:
    """
    2つのノードが「同じUI要素」とみなせるかどうか。
    tag + (name or text) + 近い座標 で判定する。
    """
    if (a.get("tag") or "").lower() != (b.get("tag") or "").lower():
        return False

    la = (a.get("name") or a.get("text") or "").strip()
    lb = (b.get("name") or b.get("text") or "").strip()
    if la != lb:
        return False

    bbox_a = node_bbox_from_raw(a)
    bbox_b = node_bbox_from_raw(b)
    cx_a, cy_a = bbox_to_center_tuple(bbox_a)
    cx_b, cy_b = bbox_to_center_tuple(bbox_b)
    return abs(cx_a - cx_b) <= tol_x and abs(cy_a - cy_b) <= tol_y


# ============================================================
# モーダル検出ベースクラス
# ============================================================

class ModalDetector:
    """
    モーダル検出のインターフェース用ベースクラス。
    すべての検出器は detect(...) / reset() を実装する。
    """
    def detect(
        self,
        nodes: List[Node],
        screen_w: int,
        screen_h: int,
        instruction: Optional[str] = None,
        min_match_ratio: float = 0.3,
        last_action_point: Optional[Tuple[int, int]] = None,
    ) -> Tuple[List[Node], List[Node], str]:
        raise NotImplementedError

    def reset(self) -> None:
        """状態をリセットしたい検出器であればオーバーライドする。"""
        pass


# ============================================================
# Diff ベースのモーダル検出
# ============================================================

class DiffModalDetector(ModalDetector):
    """
    Diffベースの検出。
    実際のロジックと状態管理は detect_modal_from_diff / _modal_diff_cache が担当。
    """
    def __init__(self):
        pass

    def reset(self):
        reset_modal_diff_cache()

    def detect(
        self,
        nodes: List[Node],
        screen_w: int,
        screen_h: int,
        instruction: Optional[str] = None,
        min_match_ratio: float = 0.3,
        last_action_point: Optional[Tuple[int, int]] = None,
    ) -> Tuple[List[Node], List[Node], str]:
        # ★ detect_modal_from_diff_global ではなく detect_modal_from_diff を呼ぶ
        return detect_modal_from_diff(
            nodes,
            screen_w,
            screen_h,
            instruction=instruction,
            min_match_ratio=min_match_ratio,
            last_action_point=last_action_point,
        )


def calculate_modal_score(
    diff_nodes: List[Node],
    last_action_point: Optional[Tuple[int, int]] = None,
) -> float:
    """
    差分ノード群が「本当にモーダルか？」を判定するスコアを計算する。
    スコアが高いほどモーダルらしい。
    """
    score = 0.0
    has_strong_role = False

    # strong ボタン候補を少し拡張
    STRONG_BUTTON_LABELS = {
        "ok", "cancel", "close", "save", "apply",
        "yes", "no", "confirm",
    }

    # モーダルっぽいキーワード（今回の sort ダイアログ向けに軽めの加点）
    MODAL_KEYWORDS = {
        "sort", "filter", "refinement", "options",
        "settings", "preferences",
    }

    # --- 1. Role / Tag / ラベルによるスコア ---
    for node in diff_nodes:
        role = (node.get("role") or "").lower()
        tag  = (node.get("tag") or "").lower()
        label = (node.get("name") or node.get("text") or "").strip()
        lower = label.lower()

        # (1) role ベース
        if role in HIGH_CONFIDENCE_ROLES:
            score += 2.0
            has_strong_role = True
        elif role in LOW_CONFIDENCE_ROLES:
            score -= 0.5

        # (2) インタラクティブ要素
        #    ※ role の有無に関係なく「UIぽさ」として加点
        if tag in INTERACTIVE_TAGS:
            score += 0.7

        # (3) strong ボタン
        if tag == "push-button" and lower in STRONG_BUTTON_LABELS:
            score += 2.0  # 1.5 → 2.0 に少し強化
            has_strong_role = True

        # (4) 文言ベースの軽い加点（sort/filterなど）
        for kw in MODAL_KEYWORDS:
            if kw in lower:
                score += 0.8    # そこまで強くないボーナス
                break

    # 強い role / 強いボタンが 1 つも無いなら大きめに減点
    if not has_strong_role:
        score -= 5.0

    # --- 2. 差分ノードの数 ---
    n_diff = len(diff_nodes)
    if n_diff < 3 and not has_strong_role:
        score -= 3.0
    elif n_diff >= 6:
        score += 1.0  # まとまった UI の出現

    # --- 3. 直前クリックとの距離（Causal Proximity） ---
    if last_action_point and diff_nodes:
        xs: List[int] = []
        ys: List[int] = []
        for n in diff_nodes:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            xs.append(cx)
            ys.append(cy)

        center_x = sum(xs) / len(xs)
        center_y = sum(ys) / len(ys)

        dist = math.hypot(center_x - last_action_point[0],
                          center_y - last_action_point[1])

        if dist < 300:
            score += 3.0
        elif dist > 900:
            score -= 1.0

    return score


def is_valid_modal(
    diff_nodes: List[Node],
    last_action_point: Optional[Tuple[int, int]] = None,
    threshold: float = 1.0,
) -> bool:
    """
    Diff で見つけたノード群を「モーダル」とみなすかどうか。
    """
    if not diff_nodes:
        return False
    score = calculate_modal_score(diff_nodes, last_action_point)
    # デバッグ用に一時的にログ出してもよい:
    print(f"[DEBUG] modal_score={score}, threshold={threshold}, n_nodes={len(diff_nodes)}")
    return score > threshold


def spatially_group_lines(
    tuples: List[Tuple[int, int, str]],
    y_threshold: int,
) -> List[str]:
    """
    (y, x, line) のタプル列を、縦方向のギャップに応じて
    '---' でグループ区切りしつつ文字列リストに変換する。
    """
    grouped: List[str] = []
    last_y: Optional[int] = None
    for y, x, line in tuples:
        if last_y is not None and (y - last_y) > y_threshold:
            grouped.append("---")
        grouped.append(line)
        last_y = y
    return grouped


def build_state_suffix(tag: str, states: List[str]) -> str:
    """
    states リストから " (CHECKED, DISABLED, ...)" のようなサフィックス文字列を作る。
    どのドメインでも同じルールで付けたいので共通化。
    """
    state_parts: List[str] = []

    if tag in ("check-box", "toggle-button"):
        state_parts.append("CHECKED" if "CHECKED" in states else "UNCHECKED")
    elif "CHECKED" in states:
        state_parts.append("CHECKED")

    if "DISABLED" in states:
        state_parts.append("DISABLED")
    if "EXPANDED" in states:
        state_parts.append("EXPANDED")
    if "COLLAPSED" in states:
        state_parts.append("COLLAPSED")
    if "FOCUSED" in states:
        state_parts.append("FOCUSED")
    if "SELECTED" in states:
        state_parts.append("SELECTED")

    return f" ({', '.join(state_parts)})" if state_parts else ""


def extract_launcher_and_status(
    nodes: List[Node],
    screen_w: int,
    screen_h: int,
) -> Tuple[List[Tuple[int, int, str]], List[Tuple[int, int, str]]]:
    """
    - 左端の Ubuntu ランチャーを APP_LAUNCHER 用に抽出
    - 画面下のステータスっぽい要素を STATUSBAR 候補として抽出
    """

    LAUNCHER_X_MAX  = int(screen_w * 0.03)   # 本当に左端だけ
    STATUSBAR_Y_MIN = int(screen_h * 0.90)

    launcher_tuples: List[Tuple[int, int, str]] = []
    status_tuples:   List[Tuple[int, int, str]] = []

    for n in nodes:
        tag = (n.get("tag") or "").lower()
        name = n.get("name") or ""
        text = n.get("text") or ""

        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

        # --- APP_LAUNCHER: Ubuntu 左ドック ---
        if tag in ("push-button", "toggle-button"):
            # x がごく左 & 縦長アイコンっぽい
            if x <= LAUNCHER_X_MAX and w <= 80 and h >= 40:
                label = name.strip() or text.strip()
                if label:
                    launcher_tuples.append(
                        (y, x, f'[launcher-app] "{label}" @ ({cx}, {cy})')
                    )
                continue  # ランチャーに入れたものは以降見ない

        # --- STATUSBAR 候補 (ラベル系だけ) ---
        if y >= STATUSBAR_Y_MIN and tag in ("label", "combo-box", "entry"):
            raw_label = (text or name).strip()
            if not raw_label:
                continue

            # " """ " みたいなクォートだけを除去
            normalized = raw_label.strip().strip('"').strip()
            if not normalized:
                continue

            # デスクトップアイコン "Home" を除外
            if normalized.lower() == "home":
                continue

            status_tuples.append(
                (y, x, f'[status] "{raw_label}" @ ({cx}, {cy})')
            )

    return launcher_tuples, status_tuples


# ============================================================
# Instruction-aware な diff ベースのモーダル検出
# ============================================================

# 直近フレームの a11y と Instruction を保持する簡易キャッシュ
_modal_diff_cache: Dict[str, Any] = {
    "instruction": None,    # type: Optional[str]
    "base_nodes": None,     # type: Optional[List[Node]]
    "modal_nodes": None,    # type: Optional[List[Node]]
}


def reset_modal_diff_cache() -> None:
    """
    必要なら外部から明示的にリセットするための関数。
    """
    global _modal_diff_cache
    _modal_diff_cache["instruction"] = None
    _modal_diff_cache["base_nodes"] = None
    _modal_diff_cache["modal_nodes"] = None


def find_new_node_clusters(
    nodes: List[Node],
    new_indices: List[int],
) -> List[Tuple[List[int], Tuple[float, float, float, float]]]:
    """
    超シンプル版:
    - new_indices 全部を「1個のクラスタ」として扱う
    - そのクラスタの中心点の bounding box を返す
    """
    if not new_indices:
        return []

    xs: List[float] = []
    ys: List[float] = []
    valid_indices: List[int] = []

    for i in new_indices:
        n = nodes[i]
        try:
            cx, cy = bbox_to_center_tuple(node_bbox_from_raw(n))
        except Exception:
            # bbox が変なノードはクラスタから除外
            continue

        xs.append(cx)
        ys.append(cy)
        valid_indices.append(i)

    if not xs or not ys or not valid_indices:
        return []

    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)

    bbox: Tuple[float, float, float, float] = (min_x, min_y, max_x, max_y)
    # いまはクラスタ1個だけ返す
    return [(valid_indices, bbox)]



def has_reasonable_modal_like_cluster(
    clusters: List[Tuple[List[int], Tuple[float, float, float, float]]],
    screen_w: int,
    screen_h: int,
) -> bool:
    if not clusters:
        return False

    screen_area = max(screen_w * screen_h, 1)
    MIN_FRAC = 0.05
    MAX_FRAC = 0.6

    for indices, (x1, y1, x2, y2) in clusters:
        if len(indices) < 3:
            continue
        area = max(x2 - x1, 0.0) * max(y2 - y1, 0.0)
        frac = area / screen_area
        if MIN_FRAC <= frac <= MAX_FRAC:
            return True

    return False


WINDOW_CONTROL_KEYWORDS = (
    "minimise", "minimize",
    "restore",
    "close",
    "maximize", "maximise",
)


def is_window_control(node: Node, screen_w: int, screen_h: int) -> bool:
    """
    画面右上にある OS / ウィンドウの Close / Minimise / Restore ボタンかどうかを判定する。
    絶対座標は使わず、相対座標と名前の一致度で判定する。
    """
    # 画面サイズが取れていない場合は判定不能として False (安全策)
    if screen_w <= 0 or screen_h <= 0:
        return False

    # 1. 基本属性の正規化
    name = (node.get("name") or node.get("text") or "").strip().lower()
    tag  = (node.get("tag")  or "").strip().lower()
    
    # 許可するタグ
    # toggle-button や graphic など、OSやスキンによって変わる可能性を考慮
    ALLOWED_TAGS = {"push-button", "button", "window-button", "toggle-button", "graphic"}
    if tag not in ALLOWED_TAGS:
        return False

    # 2. ターゲットとなる名前セット
    # ヘルプボタンなども右上にあることが多いので含める
    WINDOW_NAMES = {
        "minimise", "minimize",
        "restore",
        "close",
        "maximise", "maximize",
        "help",
    }
    
    # 部分一致ではなく、単語としての完全一致を確認する
    if name not in WINDOW_NAMES:
        return False

    # 3. 座標取得 (★修正: bbox_to_center_tuple を使用して確実に中心を取得)
    try:
        bbox = node_bbox_from_raw(node)
        cx, cy = bbox_to_center_tuple(bbox)
    except Exception:
        # 座標が取れないノードは除外できないので False
        return False

    # 4. 相対座標判定 (Resolution Independent)
    
    # Y軸: 画面の上部 8% 以内
    IS_TOP_BAND = cy < (screen_h * 0.8)

    # X軸: 画面の右側   15% 以内
    IS_RIGHT_SIDE = cx > (screen_w * 0.85)

    if IS_TOP_BAND and IS_RIGHT_SIDE:
        return True

    return False



def expand_with_close_button(
    cluster_indices: List[int],
    all_nodes: List[Node],
    max_dist: float = 80.0
) -> List[int]:
    """
    クラスタ（モーダル本体）の周辺にある孤立した「Closeボタン」を
    クラスタメンバーとして吸い込む。
    """
    if not cluster_indices:
        return []

    # 1. 現在のクラスタの Bounding Box を計算
    xs, ys = [], []
    for idx in cluster_indices:
        # 安全策: インデックスが範囲外ならスキップ
        if idx >= len(all_nodes): continue
        
        try:
            b = node_bbox_from_raw(all_nodes[idx])
            xs.append(b["x"])
            xs.append(b["x"] + b["w"])
            ys.append(b["y"])
            ys.append(b["y"] + b["h"])
        except:
            pass

    if not xs or not ys:
        return cluster_indices

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    # 2. 周辺の Close ボタンを探して追加
    expanded_indices = set(cluster_indices)
    
    # 候補となりうるタグ（テキストだけの paragraph などは除外）
    TARGET_TAGS = {"push-button", "button", "graphic", "image", "toggle-button"}

    for i, n in enumerate(all_nodes):
        if i in expanded_indices:
            continue

        tag  = (n.get("tag") or "").strip().lower()
        name = (n.get("name") or n.get("text") or "").strip().lower()

        # タグと名前でフィルタリング
        if tag not in TARGET_TAGS:
            continue
        if "close" not in name:
            # "batu" や "icon" などプロジェクト固有の閉じアイコン名があればここに追加
            continue

        # 座標チェック
        try:
            b = node_bbox_from_raw(n)
            cx = b["x"] + b["w"] / 2
            cy = b["y"] + b["h"] / 2
            
            # 拡張範囲内に入っているか？
            in_x_range = (min_x - max_dist) <= cx <= (max_x + max_dist)
            in_y_range = (min_y - max_dist) <= cy <= (max_y + max_dist)

            if in_x_range and in_y_range:
                expanded_indices.add(i)
        except:
            pass

    return list(expanded_indices)




def detect_modal_from_diff(
    curr_nodes: List[Node],
    screen_w: int,
    screen_h: int,
    instruction: Optional[str] = None,
    *,
    min_match_ratio: float = 0.3,
    last_action_point: Optional[Tuple[int, int]] = None,
) -> Tuple[List[Node], List[Node], str]:
    """
    直前フレーム(prev)との「差分」からモーダルを検出する共通ロジック。

    戻り値:
        (modal_nodes, background_nodes, mode)
        mode は "diff" or "none"
    """
    
    # Instruction が無い場合は diff ベース検出は使わない
    if instruction is None:
        reset_modal_diff_cache()
        return [], curr_nodes, "none"

    global _modal_diff_cache
    prev_instr = _modal_diff_cache.get("instruction")
    prev_base: Optional[List[Node]] = _modal_diff_cache.get("base_nodes")
    prev_modal: Optional[List[Node]] = _modal_diff_cache.get("modal_nodes") or []

    # Instruction が変わったら、prev を捨ててリセット
    if prev_instr is not None and prev_instr != instruction:
        # デバッグ用
        print(f"[DEBUG STEP 0] Instruction changed. Resetting cache.")
        prev_base = None
        prev_modal = []

    # prev_base が無い（初回 or リセット直後）なら、
    # とりあえず今回のノードをベースとして保存して終了
    if not prev_base:
        # デバッグ用
        print(f"[DEBUG STEP 0] No prev_base found (First run). Caching curr and returning.")
        _modal_diff_cache["instruction"] = instruction
        _modal_diff_cache["base_nodes"] = curr_nodes
        _modal_diff_cache["modal_nodes"] = []
        return [], curr_nodes, "none"

    prev_nodes = prev_base
    # デバッグ用
    print(f"[DEBUG STEP 0] Start Diff: prev={len(prev_nodes)}, curr={len(curr_nodes)}")

    # --------------------------------------------------------
    # 0) 前回モーダルが「まだ画面にあるか？」を確認して継承する
    # --------------------------------------------------------
    persistent_modal_nodes: List[Node] = []
    persistent_modal_indices: Set[int] = set()

    for pm in prev_modal:
        for j, cn in enumerate(curr_nodes):
            if j in persistent_modal_indices:
                continue
            if same_node(pm, cn):
                persistent_modal_nodes.append(cn)
                persistent_modal_indices.add(j)
                break
    # デバッグ用
    print(f"[DEBUG STEP 1] Persistent modal nodes: {len(persistent_modal_nodes)}")
    # --------------------------------------------------------
    # 1) prev_base / curr の対応付け
    #    （persistent_modal_indices は最初から「埋まっている」とみなす）
    # --------------------------------------------------------
    matched_prev = [False] * len(prev_nodes)
    matched_curr = [False] * len(curr_nodes)

    for j in persistent_modal_indices:
        if 0 <= j < len(matched_curr):
            matched_curr[j] = True

    for i, pn in enumerate(prev_nodes):
        for j, cn in enumerate(curr_nodes):
            if matched_curr[j]:
                continue
            if same_node(pn, cn):
                matched_prev[i] = True
                matched_curr[j] = True
                break

    
    # ========================================================
    # ★ DEBUG: Applyボタンの追跡コード (ここから)
    # ========================================================
    target_indices = []
    for i, n in enumerate(curr_nodes):
        label = (n.get("name") or n.get("text") or "").strip().lower()
        if "apply" in label:
            target_indices.append(i)
            print(f"[DEBUG TRACE] Found 'Apply' at index {i}: {n}")

    for idx in target_indices:
        is_matched = matched_curr[idx]
        print(f"[DEBUG TRACE] Index {idx} ('Apply') matched_curr = {is_matched}")
        if is_matched:
            print(f"  -> 判定: '既存ノード' (Diffではない)")
            # 既存と判定された場合、どのノードとマッチしたか知りたい場合はここを拡張可能
        else:
            print(f"  -> 判定: '新規ノード' (Diff候補)")
    # ========================================================
    # ★ DEBUG: Applyボタンの追跡コード (ここまで)
    # ========================================================


    num_prev = len(prev_nodes)
    num_match = sum(matched_prev)
    match_ratio = num_match / max(num_prev, 1)

    print("[DEBUG] match_ratio=", match_ratio, "num_prev=", num_prev, "num_curr=", len(curr_nodes))

    # ★変更: 一致率が低くても、背景(num_match)が10個以上残っていればモーダルとみなす
    MIN_PERSISTENT_BACKGROUND_NODES = 10

    # curr のノード数が極端に少ない場合（例: 15個以下）は、構造が激変していても「モーダルだけ残った」可能性があるため遷移とみなさない
    IS_SPARSE_SCREEN = len(curr_nodes) < 15 

    is_screen_transition = False
    # デバッグ用
    print(f"[DEBUG STEP 2] Match Ratio: {match_ratio:.3f} (Threshold: {min_match_ratio}), Matched Count: {num_match}")

    if match_ratio < min_match_ratio:
        if num_match < MIN_PERSISTENT_BACKGROUND_NODES and not IS_SPARSE_SCREEN:
            # 一致数が少なく、かつ画面がスカスカではないなら -> 画面遷移
            # デバッグ用
            print(f"[DEBUG STEP 2] -> DECISION: Screen Transition (Low match & Low count)")
            is_screen_transition = True
        else:
            # 一致率は低いが、背景はドッシリ残っている → 巨大モーダルの可能性大。続行！
            # デバッグ用
            print(f"[DEBUG] Low match ratio ({match_ratio:.3f}) but high persistent nodes ({num_match}). Treating as modal.")
            is_screen_transition = False
    else:
        # デバッグ用
        print(f"[DEBUG STEP 2] -> DECISION: Same Page (High match)")    

    if is_screen_transition:
        _modal_diff_cache["instruction"] = instruction
        _modal_diff_cache["base_nodes"] = curr_nodes
        _modal_diff_cache["modal_nodes"] = []
        return [], curr_nodes, "none"

    # --------------------------------------------------------
    # 2) 新しく出現したノードのみを取り出す
    # --------------------------------------------------------
    new_indices = [i for i, f in enumerate(matched_curr) if not f]
    # デバッグ用
    print(f"[DEBUG STEP 3] Raw new nodes found: {len(new_indices)}")

    # まず全ノードの中心座標を計算
    centers = [bbox_to_center_tuple(node_bbox_from_raw(n)) for n in curr_nodes]

    # ★ アプリ領域の x 範囲を決める（左 20% は OS ランチャーとみなして除外）
    #APP_AREA_X_MIN_FRAC = 0.1          # ここは後で調整可
    #app_x_min = screen_w * APP_AREA_X_MIN_FRAC

    candidate_indices = []
    for idx in new_indices:
        #cx, cy = centers[idx]
        #if cx >= app_x_min:
        candidate_indices.append(idx)

    # デバッグ用
    print(f"[DEBUG STEP 3] After Left-Side(Launcher) Filter: {len(candidate_indices)} nodes remain.")

    if not candidate_indices:
        # デバッグ用
        print(f"[DEBUG STEP 3] -> ABORT: No new nodes found.")
        _modal_diff_cache["instruction"] = instruction
        _modal_diff_cache["base_nodes"] = curr_nodes
        _modal_diff_cache["modal_nodes"] = persistent_modal_nodes
        return [], curr_nodes, "none"


    # # --------------------------------------------------------
    # # 3) 新規ノード群を空間クラスタリング
    # # --------------------------------------------------------
    # X_TOL, Y_TOL = 150.0, 100.0
    # clusters: List[List[int]] = []
    # remaining = set(new_indices)

    # while remaining:
    #     start = remaining.pop()
    #     cluster = [start]
    #     queue = [start]
    #     while queue:
    #         u = queue.pop()
    #         cx_u, cy_u = centers[u]
    #         to_add = []
    #         for v in list(remaining):
    #             cx_v, cy_v = centers[v]
    #             if abs(cx_u - cx_v) <= X_TOL and abs(cy_u - cy_v) <= Y_TOL:
    #                 to_add.append(v)
    #         for v in to_add:
    #             remaining.remove(v)
    #             queue.append(v)
    #             cluster.append(v)
    #     clusters.append(cluster)

    # # デバッグ用
    # print(f"[DEBUG STEP 4] Clusters found: {len(clusters)}")

    # #  --------------------------------------------------------
    # # 4) 「モーダルらしい」クラスタを 1 つ選ぶ
    # # --------------------------------------------------------
    # screen_area = max(screen_w * screen_h, 1)
    # MIN_CLUSTER_NODES = 2
    # MIN_CLUSTER_AREA_FRAC = 0.002   # 画面の 0.2% 以上
    # MAX_CLUSTER_AREA_FRAC = 0.6    # 画面の 60% 以下

    # best_cluster: Optional[List[int]] = None
    # best_area = 0.0

    # for cl in clusters:
    #     if len(cl) < MIN_CLUSTER_NODES:
    #         continue

    #     xs, ys = [], []
    #     for idx in cl:
    #         cx, cy = centers[idx]
    #         xs.append(cx)
    #         ys.append(cy)

    #     min_x, max_x = min(xs), max(xs)
    #     min_y, max_y = min(ys), max(ys)
    #     area = max(max_x - min_x, 0.0) * max(max_y - min_y, 0.0)
    #     frac = area / screen_area

    #     # ★ 詳細デバッグ用: なぜ弾かれたかを確認
    #     status = "OK"
    #     if len(cl) < MIN_CLUSTER_NODES: status = "REJECT(Too few nodes)"
    #     elif frac < MIN_CLUSTER_AREA_FRAC: status = "REJECT(Too small)"
    #     elif frac > MAX_CLUSTER_AREA_FRAC: status = "REJECT(Too big)"
    #     print(f"  [DEBUG STEP 5] Cluster #{i}: nodes={len(cl)}, frac={frac:.5f} -> {status}")

    #     if not (MIN_CLUSTER_AREA_FRAC <= frac <= MAX_CLUSTER_AREA_FRAC):
    #         continue
    #     # ★

    #     if area > best_area:
    #         best_area = area
    #         best_cluster = cl

    # if best_cluster is None:
    #     # デバッグ用
    #     print(f"[DEBUG STEP 5] -> ABORT: No valid cluster found.")
    #     _modal_diff_cache["instruction"] = instruction
    #     _modal_diff_cache["base_nodes"] = curr_nodes
    #     _modal_diff_cache["modal_nodes"] = persistent_modal_nodes
    #     return [], curr_nodes, "none"

    # # 孤立した Close ボタンをクラスタに吸い込む
    # # --- STEP 6: Closeボタンの吸い込み ---
    # # デバッグ用
    # print(f"[DEBUG STEP 6] Expanding cluster with close buttons...")
    # original_size = len(best_cluster)
    # best_cluster = expand_with_close_button(best_cluster, curr_nodes, max_dist=100.0)
    # print(f"  -> Expanded size: {original_size} -> {len(best_cluster)}")
    
    # フィルタを通ったノード全てを候補とします。
    best_cluster = candidate_indices

    # デバッグ用
    print(f"[DEBUG STEP 4] Treating all {len(best_cluster)} nodes as candidate cluster.")

    # 念のため Close ボタン吸い込み（candidate_indicesに含まれていれば重複無視されるので安全）
    best_cluster = expand_with_close_button(best_cluster, curr_nodes, max_dist=150.0)

    # ここで「モーダルらしさ」スコアをチェック 
    diff_nodes = [curr_nodes[i] for i in best_cluster]

    # スコア計算
    score = calculate_modal_score(diff_nodes, last_action_point)
    is_valid = score >= 1.0

    # デバッグ用
    print(f"[DEBUG STEP 7] Score: {score} (Threshold: 1.0) -> {'PASS' if is_valid else 'FAIL'}")

    if not is_valid:
        # デバッグ用
        print(f"[DEBUG STEP 7] -> ABORT: Score too low.")
        _modal_diff_cache["instruction"] = instruction
        _modal_diff_cache["base_nodes"] = curr_nodes
        _modal_diff_cache["modal_nodes"] = persistent_modal_nodes
        return [], curr_nodes, "none"
    # デバッグ用
    print(f"[DEBUG STEP 7] -> SUCCESS: Modal detected!")

    # --------------------------------------------------------
    # 5) 選ばれたクラスタの bbox を少し広げて「モーダル枠」とみなし、
    #    そこに persistent_modal も足したうえで MODAL にする
    # --------------------------------------------------------
    xs, ys = [], []
    for idx in best_cluster:
        cx, cy = centers[idx]
        xs.append(cx)
        ys.append(cy)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    MARGIN_X = 40.0
    MARGIN_Y = 40.0

    box_left   = min_x - MARGIN_X
    box_right  = max_x + MARGIN_X
    box_top    = min_y - MARGIN_Y
    box_bottom = max_y + MARGIN_Y

    print("[DEBUG detect_modal_from_diff] build modal/background, size=", len(curr_nodes))

    # ここから MODAL / BG 振り分け
    modal_nodes: List[Node] = []
    background_nodes: List[Node] = []

    diff_set = set(best_cluster)              # 今回「新規クラスタ」と判定されたノード
    modal_index_set = diff_set | set(persistent_modal_indices)  # 継承 + 新規

    # デバッグ用：どの index が候補になってるか確認
    print("[DEBUG diff_modal indices] diff_size=", len(diff_set),
          "persist_size=", len(persistent_modal_indices),
          "modal_index_set_size=", len(modal_index_set))

    for i, n in enumerate(curr_nodes):
        if i in modal_index_set:
            # ★ window control は MODAL から除外して BG に落とす
            if is_window_control(n, screen_w, screen_h):
                background_nodes.append(n)
            else:
                modal_nodes.append(n)
        else:
            background_nodes.append(n)

    # もしフィルタ後に modal_nodes が空なら、「diff でモーダルは無かった」とみなして安全に none 返し
    if not modal_nodes:
        # デバッグ用
        print(f"[DEBUG STEP 8] -> ABORT: Modal nodes empty after window_control filter.")
        _modal_diff_cache["instruction"] = instruction
        _modal_diff_cache["base_nodes"] = curr_nodes
        _modal_diff_cache["modal_nodes"] = []
        print("[DEBUG diff_modal_result] empty modal after filter -> fallback to none")
        return [], curr_nodes, "none"

    #modal_nodes をクリーニング
    modal_nodes = clean_modal_nodes(modal_nodes)

    # クリーニングの結果、空っぽになった場合のケア（任意ですが推奨）
    if not modal_nodes and not persistent_modal_nodes:
         _modal_diff_cache["instruction"] = instruction
         _modal_diff_cache["base_nodes"] = curr_nodes
         _modal_diff_cache["modal_nodes"] = []
         return [], curr_nodes, "none"

    # キャッシュを更新
    _modal_diff_cache["instruction"] = instruction
    _modal_diff_cache["base_nodes"] = background_nodes
    _modal_diff_cache["modal_nodes"] = modal_nodes

    print("[DEBUG diff_modal_result]: mode=diff modal_nodes=", len(modal_nodes),
          "background_nodes=", len(background_nodes))

    return modal_nodes, background_nodes, "diff"



class ClusterModalDetector(ModalDetector):
    """
    汎用BFSクラスタリング検出。
    """
    DEFAULT_ANCHORS = {
        "ok", "cancel", "save", "apply", "close", "done", "delete", "remove",
        "yes", "no", "agree", "accept"
    }

    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        # 1. アンカー候補探索
        anchor_indices = []
        anchor_centers = []
        all_centers = []

        for idx, n in enumerate(nodes):
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            all_centers.append((cx, cy))

            tag = (n.get("tag") or "").lower()
            if tag not in ("push-button", "link"):
                continue
            
            label = (n.get("name") or n.get("text") or "").strip().lower()
            if not label: continue

            if label in self.DEFAULT_ANCHORS:
                # ウィンドウ枠のCloseは除外
                if label == "close" and cy < screen_h * 0.1:
                    continue
                anchor_indices.append(idx)
                anchor_centers.append((cx, cy))

        if not anchor_indices:
            return [], nodes

        # 2. BFS クラスタリング
        cluster = set(anchor_indices)
        X_TOL = screen_w * 0.15
        Y_TOL = screen_h * 0.12
        
        anchor_ys = [cy for _, cy in anchor_centers]
        min_anchor_y = min(anchor_ys)
        max_anchor_y = max(anchor_ys)
        LIMIT_TOP = min_anchor_y - (screen_h * 0.2)
        LIMIT_BOTTOM = max_anchor_y + (screen_h * 0.2)

        changed = True
        while changed:
            changed = False
            for i, (cx_i, cy_i) in enumerate(all_centers):
                if i in cluster: continue
                if cy_i < LIMIT_TOP or cy_i > LIMIT_BOTTOM: continue

                for j in list(cluster):
                    cx_j, cy_j = all_centers[j]
                    if abs(cx_i - cx_j) <= X_TOL and abs(cy_i - cy_j) <= Y_TOL:
                        cluster.add(i)
                        changed = True
                        break
                if changed: break

        # 3. 分割
        if len(cluster) < 3:
            return [], nodes

        c_xs = [all_centers[i][0] for i in cluster]
        c_ys = [all_centers[i][1] for i in cluster]
        
        height = max(c_ys) - min(c_ys)
        if height > screen_h * 0.8:
            return [], nodes

        MARGIN = 40
        box_l, box_r = min(c_xs) - MARGIN, max(c_xs) + MARGIN
        box_t, box_b = min(c_ys) - MARGIN, max(c_ys) + MARGIN

        modal, bg = [], []
        for i, n in enumerate(nodes):
            cx, cy = all_centers[i]
            if i in cluster or (box_l <= cx <= box_r and box_t <= cy <= box_b):
                modal.append(n)
            else:
                bg.append(n)
        
        return modal, bg
