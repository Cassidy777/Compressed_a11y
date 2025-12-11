# core/common_ops.py
import re
import math
from typing import Dict, Any, List, Optional, Tuple, Set

Node = Dict[str, Any]


# マルチラインの name/text/description を 1 行に正規化するヘルパー
def flatten_multiline_label(s: Optional[str]) -> Optional[str]:
    """
    改行を含むラベル文字列を「1 行」に潰す。
    - 各行を strip() してからスペースで連結
    - ゼロ幅スペースなどの不可視文字も簡易的に除去
    """
    if s is None:
        return None
    if not isinstance(s, str):
        return s

    # ゼロ幅スペース系を一応削除（GIMPの変な文字対策）
    for ch in ("\u200b", "\u200e", "\u200f"):
        s = s.replace(ch, "")

    # 改行(\r\n, \n, \r)で分割して、空行を落としてから結合
    parts = re.split(r'[\r\n]+', s)
    parts = [p.strip() for p in parts if p.strip()]

    if not parts:
        return ""

    return " ".join(parts)


def normalize_multiline_fields(
    nodes: List[Node],
    keys: Tuple[str, ...] = ("name", "text", "description"),
) -> List[Node]:
    """
    各ノードについて、指定されたキー（デフォルト: name/text/description）に
    改行が含まれていれば 1 行に潰して上書きする。
    """
    for n in nodes:
        for key in keys:
            val = n.get(key)
            if isinstance(val, str) and ("\n" in val or "\r" in val):
                n[key] = flatten_multiline_label(val)
    return nodes



# 座標、サイズの文字列を str → int へ
def parse_xy(raw: str) -> Optional[Tuple[int, int]]:
    if not raw: return None
    raw = raw.strip()
    if not (raw.startswith("(") and raw.endswith(")")): return None
    try:
        x_str, y_str = raw[1:-1].split(",")
        return int(x_str.strip()), int(y_str.strip())
    except Exception:
        return None


def node_bbox_from_raw(node: Node) -> Dict[str, int]:
    raw_line = node.get("raw", "") or ""
    parts = [p.strip() for p in raw_line.split("\t")]

    # 1. まず従来ロジックを試す（元の a11y 形式もまだ来るかもしれないので）
    pos = parse_xy(parts[5]) if len(parts) >= 6 else None
    size = parse_xy(parts[6]) if len(parts) >= 7 else None

    # 2. どちらか欠けていたら、行全体から座標候補をスキャン
    if pos is None or size is None:
        coord_candidates = []
        for p in parts:
            xy = parse_xy(p)
            if xy is not None:
                coord_candidates.append(xy)

        if coord_candidates:
            # 最初の2個を pos, size として使う（1個だけなら pos のみ）
            pos = coord_candidates[0]
            if len(coord_candidates) >= 2:
                size = coord_candidates[1]

    return {
        "x": pos[0] if pos else 0,
        "y": pos[1] if pos else 0,
        "w": size[0] if size else 0,
        "h": size[1] if size else 0,
    }


# bbox → (cx,cy)
def bbox_to_center_tuple(bbox: Dict[str, int]) -> Tuple[int, int]:
    x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
    if w == 0 and h == 0: return x, y
    return x + w // 2, y + h // 2


# label が長すぎたら100文字にして末尾に "..." をつける
def truncate_label(label: str, max_len: int = 100, ellipsis: str = "...") -> str:
    if len(label) <= max_len:
        return label
    return label[: max_len - len(ellipsis)] + ellipsis


# tag と state から (CHECKED,UNCHECKED) を作成
def build_state_suffix(tag: str, states: List[str]) -> str:
    state_parts = []
    if tag in ("check-box", "toggle-button"):
        state_parts.append("CHECKED" if "CHECKED" in states else "UNCHECKED")
    elif "CHECKED" in states:
        state_parts.append("CHECKED")
    
    for s in ["DISABLED", "EXPANDED", "COLLAPSED", "FOCUSED", "SELECTED"]:
        if s in states: state_parts.append(s)
    
    return f" ({', '.join(state_parts)})" if state_parts else ""


# タグ、ラベル、中心座標が完全一致するノードが複数ある場合、最初の一個のみを残す
def dedup_same_label_same_pos(nodes: List[Node]) -> List[Node]:
    seen = set()
    result = []
    for n in nodes:
        tag = (n.get("tag") or "").lower()
        label = (n.get("name") or n.get("text") or "").strip()
        if not label:
            result.append(n)
            continue
        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        key = (tag, label, cx, cy)
        if key in seen: continue
        seen.add(key)
        result.append(n)
    return result


# 重複判定
def is_duplicate_interactive(label: str, cx: int, cy: int, sigs: set, tolerance: int = 5) -> bool:
    for sig_label, sig_cx, sig_cy in sigs:
        if label == sig_label and abs(cx - sig_cx) <= tolerance and abs(cy - sig_cy) <= tolerance:
            return True
    return False


# 画面サイズに基づき、Launcher（左端）と Statusbar（下端）を抽出
def extract_launcher_and_status(
    nodes: List[Node],
    screen_w: int,
    screen_h: int,
) -> Tuple[List[Tuple[int, int, str]], List[Tuple[int, int, str]]]:

    # 左端 3.5% (FHDで約67px)
    LAUNCHER_X_MAX  = int(screen_w * 0.035) 
    # 下端 10% (FHDで下108px)
    STATUSBAR_Y_MIN = int(screen_h * 0.90)
    
    # アイコン幅 最大5%
    ICON_W_MAX = int(screen_w * 0.05) 

    launcher_tuples = []
    status_tuples = []

    for n in nodes:
        tag = (n.get("tag") or "").lower()
        name = n.get("name") or ""
        text = n.get("text") or ""

        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

        # 左端のランチャー判定
        if tag in ("push-button", "toggle-button"):
            if x <= LAUNCHER_X_MAX and w <= ICON_W_MAX and h >= 40:
                label = (name.strip() or text.strip())
                if label:
                    launcher_tuples.append((y, x, f'[launcher-app] "{truncate_label(label)}" @ ({cx}, {cy})'))
                continue

        # 下端のステータスバー判定
        if y >= STATUSBAR_Y_MIN and tag in ("label", "combo-box", "entry"):
            raw_label = (text or name).strip()
            if not raw_label: continue
            normalized = raw_label.strip().strip('"').strip()
            if not normalized or normalized.lower() == "home": continue
            status_tuples.append((y, x, f'[status] "{truncate_label(raw_label)}" @ ({cx}, {cy})'))

    return launcher_tuples, status_tuples



# 処理済みの行リストを上から順に見て。y座標が一定以上離れたら区切り線を入れる
def spatially_group_lines(tuples: List[Tuple[int, int, str]], y_threshold: int) -> List[str]:
    grouped = []
    last_y = None
    for y, x, line in tuples:
        if last_y is not None and (y - last_y) > y_threshold:
            grouped.append("---")
        grouped.append(line)
        last_y = y
    return grouped


# 近くにある static ノード同士を　/　で繋いで結合
def merge_fragmented_static_lines(
    content_tuples: List[Tuple[int, int, str]],
    y_tol: int, 
    x_tol: int,  
) -> List[Tuple[int, int, str]]:
    pattern = re.compile(r'\[static\] "(.*)" @ \((\d+), (\d+)\)')
    static_info = []
    
    for idx, (y, x, line) in enumerate(content_tuples):
        m = pattern.match(line)
        if not m: continue
        label = m.group(1)
        cx, cy = int(m.group(2)), int(m.group(3))
        static_info.append((idx, y, x, label, cx, cy))

    if not static_info: return content_tuples

    clusters = []
    current_cluster = []
    prev_y = prev_x = None

    for item in static_info:
        idx, y, x, label, cx, cy = item
        if not current_cluster:
            current_cluster = [item]
            prev_y, prev_x = y, x
            continue
        
        if abs(y - prev_y) <= y_tol and abs(x - prev_x) <= x_tol:
            current_cluster.append(item)
            prev_y, prev_x = y, x
        else:
            clusters.append(current_cluster)
            current_cluster = [item]
            prev_y, prev_x = y, x
    if current_cluster: clusters.append(current_cluster)

    new_content = list(content_tuples)
    for cluster in clusters:
        if len(cluster) == 1: continue
        
        merged_labels = []
        for item in cluster:
            l = item[3].strip()
            if not l: continue
            skip = False
            for exist in merged_labels:
                if l == exist: skip = True; break
                if len(l) <= len(exist) and l in exist: skip = True; break
                if len(exist) < len(l) and exist in l:
                    merged_labels[merged_labels.index(exist)] = l
                    skip = True; break
            if not skip: merged_labels.append(l)
            
        if not merged_labels: continue
        
        merged_text = " / ".join(merged_labels)
        merged_text = truncate_label(merged_text)
        
        first = cluster[0]
        first_idx, first_y, first_x, _, first_cx, first_cy = first
        merged_line = f'[static] "{merged_text}" @ ({first_cx}, {first_cy})'
        new_content[first_idx] = (first_y, first_x, merged_line)
        
        for item in cluster[1:]:
            new_content[item[0]] = None

    return [x for x in new_content if x is not None]


# ============================================================================
# Helper 1: X軸方向の分割 (案3の実装)
# ============================================================================
def split_blocks_horizontally(
    blocks: List[List[Tuple[int, int, str]]],
    x_gap_threshold: int
) -> List[List[Tuple[int, int, str]]]:
    """
    Y軸で分割されたブロックリストを受け取り、
    各ブロック内でX軸方向に大きな隙間があればさらに分割して返す。
    """
    new_blocks = []

    for block in blocks:
        if not block:
            continue

        # 要素数が1つなら分割しようがないのでそのまま追加
        if len(block) < 2:
            new_blocks.append(block)
            continue

        # ブロック内の要素をX座標順にソート (x, y)
        # content_tuples = (y, x, line) なので t[1] が x
        sorted_nodes = sorted(block, key=lambda t: (t[1], t[0]))
        
        sub_blocks = []
        current_sub = [sorted_nodes[0]]
        
        for i in range(1, len(sorted_nodes)):
            prev = sorted_nodes[i-1]
            curr = sorted_nodes[i]
            
            # X座標の差分 (単純な左端同士の比較)
            dx = curr[1] - prev[1]
            
            # 隙間が閾値を超えていれば分割
            if dx > x_gap_threshold:
                sub_blocks.append(current_sub)
                current_sub = [curr]
            else:
                current_sub.append(curr)
        
        sub_blocks.append(current_sub)
        
        # 分割された各サブブロックを元のY順（上から下）に戻してから結果に追加
        for sub in sub_blocks:
            sub.sort(key=lambda t: (t[0], t[1])) 
            new_blocks.append(sub)

    return new_blocks


# ============================================================================
# Helper 2: Y軸方向の分割ロジック (ループから呼ぶために切り出し)
# ============================================================================
def _segment_content_by_y(
    content_tuples: List[Tuple[int, int, str]],
    big_gap_px: int,
    heading_section_gap_px: int
) -> List[List[Tuple[int, int, str]]]:
    """
    指定された閾値を使ってY軸方向のブロック分割を行う。
    （元の build_hierarchical_content_lines のループ内ロジックと同じ）
    """
    heading_pattern = re.compile(r'\[heading\] "(.*)" @ \((\d+), (\d+)\)')

    blocks: List[List[Tuple[int, int, str]]] = []
    current_block: List[Tuple[int, int, str]] = []

    current_block_first_heading_text: Optional[str] = None
    last_heading_y: Optional[int] = None
    last_y: Optional[int] = None

    for y, x, line in content_tuples:
        h_match = heading_pattern.match(line)
        is_heading = h_match is not None
        h_text = h_match.group(1) if is_heading else None

        if not current_block:
            current_block.append((y, x, line))
            if is_heading:
                current_block_first_heading_text = h_text
                last_heading_y = y
            last_y = y
            continue

        start_new_block = False

        # 1) 大きな Y ギャップで分割
        if last_y is not None and (y - last_y) > big_gap_px:
            start_new_block = True

        # 2) Heading によるセクション切り替え
        if is_heading:
            if current_block_first_heading_text is None:
                block_start_y = current_block[0][0]
                if (y - block_start_y) > heading_section_gap_px:
                    start_new_block = True
            else:
                if last_heading_y is not None and (y - last_heading_y) > heading_section_gap_px:
                    start_new_block = True

        if start_new_block:
            blocks.append(current_block)
            current_block = []
            current_block_first_heading_text = None
            last_heading_y = None
            
            # 新ブロックの先頭として追加
            current_block.append((y, x, line))
            if is_heading:
                current_block_first_heading_text = h_text
                last_heading_y = y
        else:
            current_block.append((y, x, line))
            if is_heading:
                last_heading_y = y

        last_y = y

    if current_block:
        blocks.append(current_block)
        
    return blocks


# ============================================================================
# Main Function: 自動調整ループ + X軸分割 (案1, 3, 6の統合)
# ============================================================================
def build_hierarchical_content_lines(
    content_tuples: List[Tuple[int, int, str]],
    big_gap_px: Optional[int] = None,
    heading_section_gap_px: Optional[int] = None,
) -> List[str]:

    if not content_tuples:
        return []

    # 1. 基本となる行間 (base_gap) を計算
    ys = sorted(y for (y, _x, _line) in content_tuples)
    dy_list = []
    for i in range(1, len(ys)):
        dy = ys[i] - ys[i - 1]
        if dy > 0:
            dy_list.append(dy)

    base_gap: int
    if dy_list:
        dy_sorted = sorted(dy_list)
        k = max(1, int(len(dy_sorted) * 0.7))
        small_dy = dy_sorted[:k]
        base_gap = small_dy[len(small_dy) // 2]
    else:
        base_gap = 20

    # 最低保証を入れる (小さすぎる計算結果を防ぐ)
    base_gap = max(base_gap, 40)

    # 2. 閾値の候補パターン (倍率)
    # 全体的に倍率を上げ、さらに広い範囲を試すように変更
    threshold_candidates = [
        (3.0, 5.0),   # 厳しめ: それでも従来の2.5倍より少し緩く
        (4.0, 6.0),   # 標準: デフォルト的な設定
        (8.0, 10.0),  # 緩め: カード内の要素を極力まとめる設定
    ]
    # 引数で固定値が指定されている場合は、それを最優先（候補1つだけにする）
    if big_gap_px is not None and heading_section_gap_px is not None:
        threshold_candidates = [("custom", "custom")]

    final_blocks = []

    # 3. 自動調整ループ
    for i, (big_mult, head_mult) in enumerate(threshold_candidates):
        
        # 閾値の決定
        if big_mult == "custom":
            curr_big = big_gap_px
            curr_head = heading_section_gap_px
        else:
            curr_big = max(int(base_gap * big_mult), 40)
            curr_head = max(int(base_gap * head_mult), curr_big + base_gap)

        # Y軸分割の実行
        temp_blocks = _segment_content_by_y(content_tuples, curr_big, curr_head)

        # 4. 過分割チェック (評価ロジック)
        is_last_try = (i == len(threshold_candidates) - 1)
        
        num_blocks = len(temp_blocks)
        # 要素数が1つだけの「孤立ブロック」の数を数える
        num_single_item_blocks = sum(1 for b in temp_blocks if len(b) == 1)
        # 孤立ブロックの割合
        single_ratio = num_single_item_blocks / max(num_blocks, 1)

        # 【判定基準】
        # - ブロック数が50を超える (多すぎ)
        # - または、ブロック数が10以上あり、かつ半分以上が「1行だけのブロック」 (バラバラすぎ)
        is_over_segmented = (num_blocks > 50) or (num_blocks > 10 and single_ratio > 0.5)

        if not is_over_segmented or is_last_try:
            # 問題ない、または最後の候補ならこれを採用してループを抜ける
            final_blocks = temp_blocks
            break
        
        # 過分割と判定されたら、次のループ（より緩い閾値）へ進む
        # print(f"[DEBUG] Over-segmented with mult={big_mult}. Retrying...")

    # 5. フォールバック処理 (総行数が少ない場合の過剰分割防止)
    # 既存コードのロジックを踏襲
    heading_pattern = re.compile(r'\[heading\] "(.*)" @ \((\d+), (\d+)\)')
    total_lines = sum(len(b) for b in final_blocks)
    has_heading_any = any(
        heading_pattern.match(line) is not None
        for block in final_blocks
        for (_y, _x, line) in block
    )
    if (not has_heading_any) and total_lines <= 30:
        merged_block = []
        for block in final_blocks:
            merged_block.extend(block)
        final_blocks = [merged_block]

    # 6. X軸分割の実行 (案3)
    # ここで「横並びのカード」などを分割する
    # 閾値は base_gap の 3.0倍 程度 (Y軸の2.5倍より少し広めにとるのがコツ)
    x_gap_threshold = max(int(base_gap * 4.0), 150)
    final_blocks = split_blocks_horizontally(final_blocks, x_gap_threshold)

    # 7. 出力文字列の生成
    out_lines = []
    for block in final_blocks:
        block_heading = None
        for _y, _x, line in block:
            m = heading_pattern.match(line)
            if m:
                block_heading = m.group(1)
                break

        if block_heading:
            out_lines.append(f'[BLOCK "{block_heading}"]')
        else:
            out_lines.append("[BLOCK]")

        for _y, _x, line in block:
            out_lines.append(line)
        out_lines.append("[/BLOCK]")

    return out_lines




# 圧縮！
def get_node_priority(node: Node) -> int:
    """
    ノードの操作性・重要性に基づく優先度を返す。
    数値が小さいほど高優先度。
    """
    tag = (node.get("tag") or "").lower()
    
    # 最高優先度: 入力・操作系 (0)
    if tag in ("entry", "combo-box", "check-box", "radio-button", "toggle-button", "input"):
        return 0 
        
    # 高優先度: クリック/遷移系 (10)
    if tag in ("push-button", "link", "menu-item", "button"):
        return 10
    
    # 中優先度: 構造系 (20)
    if tag == "heading":
        return 20 
        
    # 低優先度: 静的テキスト、コンテナ、画像など (30)
    return 30


def clean_modal_nodes(nodes: List[Node]) -> List[Node]:
    """
    検出されたモーダルノードのリストに対して最終チェックを行う。
    1. ノイズ除去（__MACOSXなど）
    2. 重複統合（座標がほぼ同じで、意味が重複するものを間引く）
    """
    import math # 必要ならファイルの先頭に移動してください

    if not nodes:
        return []

    # --- 1. 明らかなノイズのフィルタリング ---
    IGNORED_LABELS = {
        "_​_​macosx",  # ゼロ幅スペース入り
        "__macosx",   # 通常
        ".ds_store",
    }

    cleaned_phase1 = []
    for n in nodes:
        label = (n.get("name") or n.get("text") or "").strip().lower()
        if label in IGNORED_LABELS:
            continue
        
        # ラベルが空っぽ、かつインタラクティブでない要素もノイズとして捨てる
        tag = (n.get("tag") or "").lower()
        if not label and tag not in ("entry", "input", "combo-box", "check-box"):
             continue

        cleaned_phase1.append(n)

    if not cleaned_phase1:
        return []

    # --- 2. 空間的・意味的重複の統合 (Deduplication) ---
    n_count = len(cleaned_phase1)
    meta_list = []
    
    for i, n in enumerate(cleaned_phase1):
        try:
            # 同じファイル内にあるのでそのまま呼べる
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
        except:
            cx, cy = 0, 0
        
        text = (n.get("name") or n.get("text") or "").strip().lower()
        prio = get_node_priority(n)
        meta_list.append({
            "idx": i,
            "cx": cx, "cy": cy,
            "text": text,
            "prio": prio,
            "removed": False
        })

    SAME_POS_THRESHOLD = 5.0

    for i in range(n_count):
        if meta_list[i]["removed"]: continue
        a = meta_list[i]
        
        for j in range(i + 1, n_count):
            if meta_list[j]["removed"]: continue
            b = meta_list[j]

            dist = math.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"])
            if dist > SAME_POS_THRESHOLD:
                continue

            is_related = (a["text"] == b["text"]) or (a["text"] in b["text"]) or (b["text"] in a["text"])
            if not a["text"] and not b["text"]:
                is_related = False

            if is_related:
                if a["prio"] < b["prio"]:
                    b["removed"] = True
                elif b["prio"] < a["prio"]:
                    a["removed"] = True
                    break
                else:
                    if len(a["text"]) >= len(b["text"]):
                        b["removed"] = True
                    else:
                        a["removed"] = True
                        break

    final_nodes = []
    for meta, node in zip(meta_list, cleaned_phase1):
        if not meta["removed"]:
            final_nodes.append(node)

    return final_nodes



def dedup_similar_nodes_by_priority(
    nodes: List[Node],
    distance_threshold: float = 20.0, # 許容する中心座標の距離（ピクセル）
) -> List[Node]:
    """
    近接し、かつラベルが類似・重複するノード群を、優先度（get_node_priority）に基づいて間引く。
    """
    if not nodes:
        return []

    # 1. ノードのメタ情報を準備
    meta_list = []
    for i, n in enumerate(nodes):
        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        
        label = (n.get("name") or n.get("text") or "").strip()
        prio = get_node_priority(n)
        
        meta_list.append({
            "idx": i, "node": n, 
            "cx": cx, "cy": cy,
            "label": label.lower(), 
            "prio": prio,
            "removed": False
        })
    
    n_count = len(meta_list)

    # 2. 優先度と近接性に基づく重複排除ロジック
    for i in range(n_count):
        if meta_list[i]["removed"]: continue
        a = meta_list[i]
        
        for j in range(i + 1, n_count):
            if meta_list[j]["removed"]: continue
            b = meta_list[j]

            # 座標が近いか？ (ユークリッド距離)
            dist = math.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"])

            is_duplicate_candidate = False
            
            if a["label"] == b["label"]:
                # ラベルが完全一致する場合、Y座標が近ければ重複と見なす (X方向のズレを許容)
                if abs(a["cy"] - b["cy"]) <= 30.0:
                    is_duplicate_candidate = True
            elif dist <= distance_threshold:
                 # ラベルが包含関係にある場合、近接座標 (20.0px) の制約を維持
                 is_duplicate_candidate = True

            if not is_duplicate_candidate:
                continue

            # ラベルが関連しているか？ (完全一致 or 包含関係)
            # ラベルがないノード同士は、この関数では削除しない（他のフィルターに委譲）
            if not a["label"] or not b["label"]:
                is_label_related = False
            else:
                is_label_related = (
                    a["label"] == b["label"]
                    or a["label"] in b["label"]
                    or b["label"] in a["label"]
                )
            
            if is_label_related:
                # ★ 追加: ラベル長が極端に違う場合は「別物」とみなしてスキップ
                longer = max(len(a["label"]), len(b["label"]))
                shorter = min(len(a["label"]), len(b["label"]))
                # 例: 長い方 >= 60 文字 & 長さ比 <= 0.5 のときは重複扱いしない
                if longer >= 60 and shorter / max(longer, 1) <= 0.5:
                    continue

                # ★ 追加: link と static のペアなら、必ず link 側を残す
                tag_a = (a["node"].get("tag") or "").lower()
                tag_b = (b["node"].get("tag") or "").lower()
                if "link" in {tag_a, tag_b}:
                    print("[DEBUG] found link pair:", tag_a, tag_b, a["label"], b["label"])
                if {"link", "static"} == {tag_a, tag_b}:
                    if tag_a == "link":
                        b["removed"] = True
                    else:
                        a["removed"] = True
                    continue

                # 優先度が低い方を削除（prioが大きい方が敗者）
                if a["prio"] < b["prio"]:
                    b["removed"] = True
                elif b["prio"] < a["prio"]:
                    a["removed"] = True
                    break # aが削除されたので、次のiに進む
                else:
                    # 優先度が同じ場合: ラベルの短い方（情報が少ない方）を削除
                    if len(a["label"]) < len(b["label"]):
                        a["removed"] = True
                        break
                    else:
                        b["removed"] = True

    return [meta["node"] for meta in meta_list if not meta["removed"]]



def dedup_heading_and_static(
    nodes: List[Node],
    y_tolerance: int = 15,
) -> List[Node]:
    """
    Content領域で、'heading' タグと同一のラベルを持ち、かつY座標が近い 
    'static' ノードを、'heading' ノードを残して削除する。
    """
    headings_meta: List[Dict[str, Any]] = []
    
    # 1. headingノードの情報を収集
    for n in nodes:
        tag = (n.get("tag") or "").lower()
        label = (n.get("name") or n.get("text") or "").strip()
        if tag == "heading" and label:
            try:
                bbox = node_bbox_from_raw(n)
                cy = bbox_to_center_tuple(bbox)[1]
                # 小文字のラベルと中心Y座標を保持
                headings_meta.append({"label": label.lower(), "cy": cy}) 
            except:
                pass
                
    if not headings_meta:
        return nodes

    result_nodes = []
    for n in nodes:
        tag = (n.get("tag") or "").lower()
        label = (n.get("name") or n.get("text") or "").strip()
        
        # headingノードは常に残す
        if tag == "heading":
            result_nodes.append(n)
            continue
            
        # staticノードが重複しているかチェック
        if tag == "static" and label:
            try:
                bbox = node_bbox_from_raw(n)
                cy = bbox_to_center_tuple(bbox)[1]
                
                # 既存のheadingと照合
                is_duplicate = False
                for h in headings_meta:
                    if h["label"] == label.lower() and abs(cy - h["cy"]) <= y_tolerance:
                        is_duplicate = True
                        break
                        
                if is_duplicate:
                    # 重複しているstaticは削除
                    continue

            except:
                pass
        
        result_nodes.append(n)
        
    return result_nodes


def dedup_horizontal_menu_nodes(nodes: List[Node], eps_x: int = 20, eps_y: int = 20) -> List[Node]:
    """
    MENUBAR などの「横一列に並んだ menu 要素」で、
    - ラベルが同じ
    - 位置がほぼ同じ (x, y の差が小さい)
    ノードを 1 つにマージする。

    例:
        [menu] "File" @ (90, 74)
        [menu] "File" @ (90, 76)
    → どちらか 1 つだけ残す。

    eps_x, eps_y は「同じ要素とみなす許容範囲（px）」。
    """
    from .common_ops import node_bbox_from_raw  # 既に同ファイルにあれば不要

    items = []
    for n in nodes:
        bbox = node_bbox_from_raw(n)
        x, y = bbox["x"], bbox["y"]
        name = (n.get("name") or n.get("text") or "").strip()
        items.append((x, y, name, n))

    # 左 → 右、上 → 下 の順に並べる
    items.sort(key=lambda t: (t[0], t[1]))

    deduped: List[Node] = []
    last = None

    for x, y, name, node in items:
        if last is None:
            last = (x, y, name, node)
            continue

        lx, ly, lname, lnode = last

        # ラベルが同じ かつ 位置がほぼ同じ → 同じメニュー項目とみなす
        if name == lname and abs(x - lx) <= eps_x and abs(y - ly) <= eps_y:
            # どちらを残すか:
            last_text = (lnode.get("name") or lnode.get("text") or "").strip()
            curr_text = (node.get("name") or node.get("text") or "").strip()

            # 「中身がある方」を優先
            if curr_text and not last_text:
                last = (x, y, name, node)
            elif curr_text and last_text and y < ly:
                # 両方中身ありなら、より上にある方
                last = (x, y, name, node)
            # それ以外は last を維持（= 今の node は捨てる）
        else:
            # ここまでの last を確定
            deduped.append(lnode)
            last = (x, y, name, node)

    # 最後の1個を追加
    if last is not None:
        deduped.append(last[3])

    return deduped