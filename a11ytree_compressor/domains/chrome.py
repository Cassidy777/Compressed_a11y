import re
from typing import List, Dict, Any, Tuple, Optional, Set
from urllib.parse import urlparse, parse_qs, unquote
from statistics import median

from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    merge_fragmented_static_lines, build_hierarchical_content_lines,
    truncate_label, dedup_same_label_same_pos, build_state_suffix, clean_modal_nodes
)
from ..core.modal_strategies import ModalDetector, ClusterModalDetector


# ============================================================================
# 1. Chrome-Specific Constants
# ============================================================================


# Modal anker
COOKIE_BUTTON_ANCHORS = {
    "Accept Cookies", "Reject Non-Essential Cookies", 
    "Cookies Settings", "Cookie Settings", "Accept all", "Reject all"
}

COOKIE_TEXT_KEYWORDS = ("cookie", "cookies", "privacy", "クッキー", "プライバシー")

INTERACTIVE_TAGS = {
    "push-button", "menu-item", "combo-box", "list",
    "list-item", "entry", "check-box", "radio-button", "link", "toggle-button"
}

# chrome anker
BROWSER_UI_ANCHOR_BUTTONS = {
    "reload",
    "you",
    "chrome",
    "bookmark this tab",
    "back",                   
    "view site information",  
    "extensions",             
    "side panel",
}

BROWSER_UI_ANCHOR_ENTRIES = {
    "address and search bar",
}

BROWSER_TAB_ANCHORS = {
    "search tabs",
    "new tab",
    "close",   # タブの ×
}

WINDOW_CONTROL_NAMES = {
    "minimise", "minimize", "restore", "maximize", "close",
}

# ============================================================================
# 2. Chrome-Specific Modal Detectors (Strategies)
# ============================================================================

class CookieBannerDetector(ModalDetector):
    """
    画面下部の Cookie 同意バナー、または中央の Cookie 同意モーダルを検出する。
    """

    def __init__(self, debug: bool = False):
        self.debug = debug

    def _short_node_repr(self, n: Node) -> str:
        tag = (n.get("tag") or "").lower()
        label = (n.get("name") or n.get("text") or "")
        return f"{tag} | {label[:40]!r}"

    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        # 修正1: 画面中央も拾えるように、ヘッダー領域(上部20%)のみを除外する
        SEARCH_START_Y = int(screen_h * 0.20)

        if self.debug:
            print("\n================ CookieBannerDetector DEBUG ================")
            print(f"screen_w={screen_w}, screen_h={screen_h}, num_nodes={len(nodes)}")
            print(f"SEARCH_START_Y={SEARCH_START_Y}")

        # インデックスと中心座標を収集
        candidates: List[dict] = []
        
        for idx, n in enumerate(nodes):
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)

            if cy < SEARCH_START_Y:
                continue

            tag = (n.get("tag") or "").lower()
            label = (n.get("name") or n.get("text") or "").strip()
            lower = label.lower()

            is_anchor = False
            is_related = False

            # BUTTON/LINK で COOKIE_BUTTON_ANCHORS に完全一致するもの
            if tag in ("push-button", "link") and label in COOKIE_BUTTON_ANCHORS:
                is_anchor = True
                is_related = True

            # テキストに "cookie" "consent" "privacy" などのキーワードが入っている
            if any(kw in lower for kw in COOKIE_TEXT_KEYWORDS):
                is_related = True
                if tag in ("push-button", "link"):
                    is_anchor = True

            if is_related:
                candidates.append({
                    "idx": idx,
                    "cy": cy,
                    "cx": cx,
                    "is_anchor": is_anchor
                })

        # (1) 候補が少なすぎる場合は却下
        if len(candidates) < 2:
            if self.debug:
                print("[FILTER] Too few related nodes (<2) → reject Cookie banner")
            return [], nodes

        # 修正2: クラスタリング (Y座標でグループ分け)
        # 中央のモーダルと、フッターの「Privacy Policy」リンクが離れている場合に分離する
        candidates.sort(key=lambda x: x["cy"])
        
        clusters = []
        current_cluster = [candidates[0]]
        
        # Y座標が画面高さの10%以上離れていたら別グループとする
        Y_GAP_THRESHOLD = screen_h * 0.10
        
        for i in range(1, len(candidates)):
            curr = candidates[i]
            prev = candidates[i-1]
            if (curr["cy"] - prev["cy"]) > Y_GAP_THRESHOLD:
                clusters.append(current_cluster)
                current_cluster = []
            current_cluster.append(curr)
        clusters.append(current_cluster)

        # ベストなクラスタを選定
        # 評価基準: アンカー(ボタン)を含む数を優先、同数ならY座標が大きい(下にある)ものを優先
        best_cluster = None
        max_score = -1

        for cluster in clusters:
            anchor_count = sum(1 for c in cluster if c["is_anchor"])
            avg_y = sum(c["cy"] for c in cluster) / len(cluster)
            
            # スコアリング: アンカー数 * 10000 + Y座標 (下にあるほど有利)
            score = anchor_count * 10000 + avg_y
            
            if score > max_score:
                max_score = score
                best_cluster = cluster

        if not best_cluster or len(best_cluster) < 2:
            if self.debug:
                print("[FILTER] No valid cluster found")
            return [], nodes

        # 選ばれたクラスタのインデックスセット
        target_indices = {c["idx"] for c in best_cluster}
        
        # デバッグ出力
        if self.debug:
            print(f"[STEP1] clusters_count={len(clusters)}, best_cluster_size={len(best_cluster)}")
            anchor_indices = [c["idx"] for c in best_cluster if c["is_anchor"]]
            print(f"[STEP1] best_cluster_anchors={anchor_indices}")

        # (2) 'privacy' だけの場合は Cookie バナーではない (厳しすぎる場合調整)
        def is_privacy_only(i):
            t = (nodes[i].get("name") or nodes[i].get("text") or "").lower()
            return "privacy" in t and all(kw not in t for kw in ["cookie", "consent"])

        if all(is_privacy_only(i) for i in target_indices):
            if self.debug:
                print("[FILTER] privacy-only pattern → reject Cookie banner")
            return [], nodes

        # 2. バウンディングボックス計算
        cluster_cxs = [c["cx"] for c in best_cluster]
        cluster_cys = [c["cy"] for c in best_cluster]
        
        min_cx, max_cx = min(cluster_cxs), max(cluster_cxs)
        min_cy, max_cy = min(cluster_cys), max(cluster_cys)

        MARGIN_X = int(screen_w * 0.05) # マージン少し縮小
        MARGIN_Y = 40
        box_l, box_r = min_cx - MARGIN_X, max_cx + MARGIN_X
        box_t, box_b = min_cy - MARGIN_Y, max_cy + MARGIN_Y

        # 画面との重なり具合の参考用
        box_w = box_r - box_l
        box_h = box_b - box_t
        area_ratio = (box_w * box_h) / (screen_w * screen_h + 1e-9)

        # 修正3: 面積閾値の緩和 (0.05 -> 0.01)
        # 高解像度(5100px)の場合、中央の小さなモーダルは0.5%程度になることもあるため
        if area_ratio < 0.01:
            if self.debug:
                print(f"[FILTER] area_ratio={area_ratio:.4f} < 0.01 → reject Cookie banner")
            return [], nodes

        if self.debug:
            print(f"[STEP2] modal_box=(l={box_l}, t={box_t}, r={box_r}, b={box_b}), "
                  f"size=({box_w}x{box_h}), area_ratio={area_ratio:.4f}")

        # 3. 分割
        modal: List[Node] = []
        bg: List[Node] = []

        for idx, n in enumerate(nodes):
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            
            is_in_box = box_l <= cx <= box_r and box_t <= cy <= box_b

            tag = (n.get("tag") or "").lower()
            label = (n.get("name") or n.get("text") or "").strip().lower()

            # ターゲットに含まれる、またはボックス内の Close ボタン
            is_target = (idx in target_indices) or (tag == "push-button" and label == "close")
            
            # ボックス内にあり、かつターゲット群に関連するか、インタラクティブ要素であれば巻き込む
            # (単純な包含判定だと背景の巨大DIVなどを巻き込むリスクがあるため、少し条件をつける)
            should_include = False
            if is_in_box:
                if is_target:
                    should_include = True
                elif idx in target_indices:
                    should_include = True
                # ボックス内のボタンやリンクは、Cookieバナーの一部である可能性が高いので含める
                elif tag in ("push-button", "link", "check-box", "toggle-button"):
                    should_include = True
                # ボックス内のテキストも、短いものなら説明文として含める
                elif tag in ("static", "paragraph", "heading") and len(label) < 200:
                    should_include = True

            if should_include:
                modal.append(n)
            else:
                bg.append(n)

        return modal, bg




# ============================================================================
# 2. Chrome-Specific Modal Detectors (Strategies)
# ============================================================================
# ★ 追加: 画面中央に要素が凝縮しているパターン（Newsletter等）を検出するクラス
class CenteredOverlayDetector(ModalDetector):
    """
    「画面中央に要素が密集している」という構造的特徴に基づいてポップアップを検出する。
    キーワードは必須とせず、位置・サイズ・密度（インタラクティブ要素の含有）で判定する。
    """

    def __init__(self, debug: bool = False):
        self.debug = debug

    def _short_node_repr(self, n: Node) -> str:
        tag = (n.get("tag") or "").lower()
        label = (n.get("name") or n.get("text") or "")[:30]
        return f"{tag}: {label!r}"

    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        if not nodes:
            return [], nodes

        if self.debug:
            print("\n================ CenteredOverlayDetector DEBUG ================")
            print(f"screen_w={screen_w}, screen_h={screen_h}, num_nodes={len(nodes)}")

        # 1. 簡易クラスタリング
        DIST_THRESHOLD = min(screen_w, screen_h) * 0.12

        centers = []
        valid_indices = []
        for i, n in enumerate(nodes):
            try:
                bbox = node_bbox_from_raw(n)
                cx = bbox["x"] + bbox["w"] // 2
                cy = bbox["y"] + bbox["h"] // 2
                if 0 <= cx <= screen_w and 0 <= cy <= screen_h:
                    centers.append((cx, cy))
                    valid_indices.append(i)
            except Exception as e:
                if self.debug:
                    print(f"[SKIP NODE] idx={i}, error={e}")
                continue

        if not centers:
            if self.debug:
                print("no valid centers → return no modal")
            return [], nodes

        n_points = len(centers)
        visited = [False] * n_points
        clusters: List[List[int]] = []

        for i in range(n_points):
            if visited[i]:
                continue
            cluster_group = [valid_indices[i]]
            queue = [i]
            visited[i] = True

            while queue:
                curr_idx = queue.pop(0)
                cx1, cy1 = centers[curr_idx]
                for j in range(n_points):
                    if visited[j]:
                        continue
                    cx2, cy2 = centers[j]
                    dist = ((cx1 - cx2)**2 + (cy1 - cy2)**2)**0.5
                    if dist < DIST_THRESHOLD:
                        visited[j] = True
                        queue.append(j)
                        cluster_group.append(valid_indices[j])

            clusters.append(cluster_group)

        if self.debug:
            print(f"num_clusters={len(clusters)}")

        # 2. クラスタ評価
        best_cluster = None
        best_score = -1.0

        screen_cx, screen_cy = screen_w / 2, screen_h / 2
        max_dist = ((screen_w/2)**2 + (screen_h/2)**2)**0.5

        BONUS_KEYWORDS = {"subscribe", "sign", "login", "register", "join", "search", "agree"}
        total_nodes = len(nodes)

        best_bbox = None  # 吸収フェーズ用

        for c_idx, group in enumerate(clusters):
            debug_info = {
                "cluster_idx": c_idx,
                "num_nodes": len(group),
                "interactive_count": 0,
                "has_keyword": False,
                "reason": [],
            }

            if len(group) < 3:
                debug_info["reason"].append("len<3 (too small)")
                if self.debug:
                    print(f"[CLUSTER {c_idx}] SKIP early: {debug_info}")
                continue

            xs, ys = [], []
            interactive_count = 0
            has_bonus_keyword = False

            for idx in group:
                n = nodes[idx]
                bbox = node_bbox_from_raw(n)
                xs.extend([bbox["x"], bbox["x"] + bbox["w"]])
                ys.extend([bbox["y"], bbox["y"] + bbox["h"]])

                tag = (n.get("tag") or "").lower()
                label = (n.get("name") or n.get("text") or "").lower()

                if tag in ("push-button", "entry", "input", "link", "check-box"):
                    interactive_count += 1
                if any(k in label for k in BONUS_KEYWORDS):
                    has_bonus_keyword = True

            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            w = max_x - min_x
            h = max_y - min_y
            cx = min_x + w / 2
            cy = min_y + h / 2

            area_ratio = (w * h) / (screen_w * screen_h + 1e-9)
            dist_from_center = ((cx - screen_cx)**2 + (cy - screen_cy)**2)**0.5
            center_ratio = dist_from_center / (max_dist + 1e-9)
            cluster_ratio = len(group) / (total_nodes + 1e-9)

            # アスペクト比（幅 / 高さ）: ヘッダー検出用
            aspect_ratio = w / (h + 1e-9)

            debug_info.update({
                "bbox": (min_x, min_y, w, h),
                "area_ratio": round(area_ratio, 4),
                "center_ratio": round(center_ratio, 4),
                "cluster_ratio": round(cluster_ratio, 4),
                "aspect_ratio": round(aspect_ratio, 2), # デバッグ用に追加
                "interactive_count": interactive_count,
                "has_keyword": has_bonus_keyword,
            })

            w_ratio = w / (screen_w + 1e-9)  # 幅の占有率
            h_ratio = h / (screen_h + 1e-9)  # 高さの占有率

            # --- (A) フィルタリング (Hard Filter) ---
            # ここで is_rejected フラグを立て、Trueなら即座に continue する
            is_rejected = False

            # 1. ページ全体 (≳98%) を除外
            if w > screen_w * 0.98 and h > screen_h * 0.98:
                debug_info["reason"].append("too_large (page-size)")
                is_rejected = True

            # 2. 面積が小さすぎる (≲5%) ものも除外
            if area_ratio < 0.05:
                debug_info["reason"].append("too_small_area (<5%)")
                is_rejected = True

            # 3. 高さが小さすぎる（ヘッダー帯など）はモーダルではない
            # 【重要】失敗ケース(h_ratio=0.11) を弾く
            h_ratio = h / (screen_h + 1e-9)
            if h_ratio < 0.2:
                debug_info["reason"].append("too_short_height (<20% screen)")
                is_rejected = True

            # 4. 極端に横長（アスペクト比 > 5.0）はヘッダー/バナーとみなす
            # 【追加】失敗ケース(aspect=15.1) を弾く。成功ケースは aspect=1.9 なので影響なし
            if aspect_ratio > 5.0:
                debug_info["reason"].append(f"too_flat_aspect_ratio ({aspect_ratio:.1f})")
                is_rejected = True

            # 5. 画面のかなりの部分を覆い、かつノード数も全体の大半 → ページ本体
            is_full_screen_coverage = (w > screen_w * 0.90) and (h > screen_h * 0.90)
            if cluster_ratio > 0.60:
                # ノード数が多く、かつ「横幅が画面いっぱい(>92%)」の場合はページ本体とみなす
                # (Delta航空のような巨大モーダルは幅67%程度なので、ここを通過する)
                if w_ratio > 0.92:
                    debug_info["reason"].append(f"too_many_nodes_wide_body (w={w_ratio:.2f})")
                    is_rejected = True
                
                # ケース3: 全画面(幅も高さも)をほぼ覆っている場合
                elif w_ratio > 0.90 and h_ratio > 0.90:
                    debug_info["reason"].append("too_many_nodes (fullscreen cover)")
                    is_rejected = True


            # 6. 面積はかなり大きいのにノード数が少ない → レイアウト用コンテナとみなす
            # 【重要】失敗ケースの Cluster 1 (Backdrop/Body) を弾く
            if area_ratio > 0.7 and cluster_ratio < 0.2:
                debug_info["reason"].append("sparse_big_region (layout container)")
                is_rejected = True

            # 7. インタラクティブ要素がない
            if interactive_count == 0:
                debug_info["reason"].append("no_interactive_element")
                is_rejected = True

            # 8. 画面中心から遠すぎる
            if center_ratio > 0.35:
                debug_info["reason"].append("too_far_from_center_band")
                is_rejected = True

            # フィルタに引っかかったら評価をスキップ
            if is_rejected:
                if self.debug:
                    print(f"[CLUSTER {c_idx}] REJECTED: {debug_info}")
                continue

            # (C) スコアリング（ここまで到達したものは「モーダル候補」として有力）
            center_score = 50 * (1.0 - (dist_from_center / max_dist))
            density_score = min(len(group), 30)
            keyword_score = 10 if has_bonus_keyword else 0
            total_score = center_score + density_score + keyword_score

            debug_info.update({
                "center_score": round(center_score, 2),
                "density_score": density_score,
                "keyword_score": keyword_score,
                "total_score": round(total_score, 2),
            })

            if self.debug:
                print(f"[CLUSTER {c_idx}] PASSED: {debug_info}")

            if total_score > best_score:
                best_score = total_score
                best_cluster = group
                best_bbox = (min_x, max_x, min_y, max_y)

        if self.debug:
            if best_cluster is None:
                print(">>> RESULT: no modal detected (best_cluster=None)")
            else:
                print(">>> RESULT: best_cluster selected")
                print(f"best_score={best_score}")
                print(f"best_cluster_size={len(best_cluster)}")
                for idx in best_cluster:
                    n = nodes[idx]
                    try:
                        bbox = node_bbox_from_raw(n)
                        print(f"  - node_idx={idx}, {self._short_node_repr(n)}, "
                              f"bbox=({bbox['x']},{bbox['y']},{bbox['w']},{bbox['h']})")
                    except Exception:
                        pass

        # 3. 吸収フェーズ
        if best_cluster and best_bbox is not None:
            cluster_set = set(best_cluster)
            modal: List[Node] = []
            bg: List[Node] = []

            min_x, max_x, min_y, max_y = best_bbox
            EXPAND = 20
            mx1 = min_x - EXPAND
            mx2 = max_x + EXPAND
            my1 = min_y - EXPAND
            my2 = max_y + EXPAND

            absorbed = []

            for i, n in enumerate(nodes):
                bbox = node_bbox_from_raw(n)
                cx = bbox["x"] + bbox["w"] // 2
                cy = bbox["y"] + bbox["h"] // 2

                if (i in cluster_set) or (mx1 <= cx <= mx2 and my1 <= cy <= my2):
                    modal.append(n)
                    if i not in cluster_set:
                        absorbed.append(i)
                else:
                    bg.append(n)

            if self.debug:
                print(f"[ABSORB] absorbed {len(absorbed)} nodes: {absorbed}")

            return modal, bg

        return [], nodes




# ============================================================================
# 3. Chrome Compressor Implementation
# ============================================================================

class ChromeCompressor(BaseA11yCompressor):
    domain_name = "chrome"

    def get_modal_detectors(self) -> List[ModalDetector]:
        return [
            # ★ 1. Diffで見つからなかった場合の「中央ポップアップ」検出 (最強)
            CenteredOverlayDetector(),
            
            # 2. それでもなければ汎用クラスタ (誤検知リスクあり、順序は要調整)
            # ClusterModalDetector(), 
            
            # 3. 専用UI検出
            CookieBannerDetector(),
            FloatingMenuDetector(),
            FullscreenOverlayDetector(),
        ]


class FullscreenOverlayDetector(ModalDetector):
    """
    Delta航空のような全画面オーバーレイ（Close Dialog ... Confirm）を検出。
    """
    def __init__(self, debug: bool = False):
        self.debug = debug

    def detect(
        self,
        nodes: List[Node],
        screen_w: int,
        screen_h: int
    ) -> Tuple[List[Node], List[Node]]:
        if not nodes:
            return [], nodes

        import re  # 念のためここで

        TOP_MIN_Y = int(screen_h * 0.05)
        TOP_MAX_Y = int(screen_h * 0.80)
        BOT_MIN_Y = int(screen_h * 0.20)

        TOP_ANCHORS = {
            "close", "close dialog", "dismiss", "cancel", "done",
            "back", "return", "exit", "×", "x", "✕"
        }
        BOT_ANCHORS = {
            "confirm", "confirm my choices", "accept", "accept all", "save",
            "save preferences", "agree", "allow", "continue", "submit",
            "apply", "ok", "yes", "no", "reject", "reject all", "decline"
        }

        TARGET_TAGS = {
            "push-button", "link", "toggle-button", "image", "graphic", "button"
        }

        top_centers: List[int] = []
        bot_centers: List[int] = []

        # 1. アンカー探索
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag not in TARGET_TAGS:
                continue

            label = (n.get("name") or n.get("text") or "").strip().lower()
            if not label:
                continue

            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)

            # 左端 5% を除外（ハンバーガーメニューなどの誤爆防止）
            if cx < screen_w * 0.05:
                continue

            label_words = set(re.split(r"[\s_\-]+", label))

            is_top = False
            # Top anchors
            if TOP_MIN_Y <= cy <= TOP_MAX_Y:
                if (label in TOP_ANCHORS) or (label_words & TOP_ANCHORS):
                    top_centers.append(cy)
                    is_top = True

            # Bottom anchors
            if (not is_top) and cy >= BOT_MIN_Y:
                if ((label in BOT_ANCHORS)
                    or (label_words & BOT_ANCHORS)
                    or ("confirm my choices" in label)
                    or ("save preferences" in label)):
                    bot_centers.append(cy)

        if not top_centers or not bot_centers:
            # アンカーが両方なければオーバーレイとはみなさない
            return [], nodes

        # 2. 垂直範囲決定
        top_y = min(top_centers) - 40
        bot_y = max(bot_centers) + 40
        height = bot_y - top_y

        # 高さが小さすぎる → 単なるバナー等
        if height < screen_h * 0.3:
            if self.debug:
                print(f"[Fullscreen] REJECT: Height too small ({height})")
            return [], nodes

        # 3. バンド内 / 外で分割
        candidate_modal: List[Node] = []
        candidate_bg: List[Node] = []

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            _, cy = bbox_to_center_tuple(bbox)
            if top_y <= cy <= bot_y:
                candidate_modal.append(n)
            else:
                candidate_bg.append(n)

        total_nodes = len(nodes)
        modal_nodes_count = len(candidate_modal)
        node_ratio = modal_nodes_count / (total_nodes + 1e-9)

        # 【追加】候補領域の横幅を計算
        if candidate_modal:
            xs = []
            for n in candidate_modal:
                bbox = node_bbox_from_raw(n)
                xs.extend([bbox["x"], bbox["x"] + bbox["w"]])
            min_x, max_x = min(xs), max(xs)
            modal_width = max_x - min_x
            width_ratio = modal_width / (screen_w + 1e-9)
        else:
            width_ratio = 0.0

        interactive_count = sum(
            1 for n in candidate_modal
            if (n.get("tag") or "").lower()
            in {"push-button", "link", "input", "entry", "check-box"}
        )

        debug_info = {
            "top_y": top_y,
            "bot_y": bot_y,
            "height": height,
            "node_ratio": round(node_ratio, 3),
            "width_ratio": round(width_ratio, 3), # デバッグ用
            "modal_count": modal_nodes_count,
            "interactive_count": interactive_count,
        }

# 条件1: ノード含有率が極端に高い (80%以上) 場合
        if node_ratio > 0.80:
            if self.debug:
                print(f"[Fullscreen] REJECT: Dominant Node Count (>80%) {debug_info}")
            return [], nodes

        # 条件2: 「高さ」と「ノード数」がある程度大きい場合
        if height > screen_h * 0.60 and node_ratio > 0.60:
            if width_ratio > 0.90:
                if self.debug:
                    print(f"[Fullscreen] REJECT: Large Area & Full Width (Likely Page Body) {debug_info}")
                return [], nodes
            else:
                pass

        # 条件3: インタラクティブ要素がほとんどない大きな領域は記事ページ誤検知の可能性
        # (ここで計算していた処理は上に移動済みなので、判定だけ残す)
        if modal_nodes_count > 20 and (interactive_count / (modal_nodes_count + 1e-9)) < 0.05:
            if self.debug:
                print(f"[Fullscreen] REJECT: Low interactivity {debug_info}")
            return [], nodes

        if self.debug:
            print(f"[Fullscreen] ACCEPT: {debug_info}")

        return candidate_modal, candidate_bg



class FloatingMenuDetector(ModalDetector):
    """
    右上の '...' メニューやコンテキストメニューを検出。
    """
    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        # 右半分にある menu or menu-item の集合体を探す
        candidates = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            bbox = node_bbox_from_raw(n)
            
            if (tag == "menu" or role == "menu") and bbox["x"] > screen_w * 0.4:
                candidates.append(bbox)
        
        if not candidates:
            return [], nodes
            
        # 最大のメニュー領域を採用
        best_menu = max(candidates, key=lambda b: b["w"] * b["h"])
        
        # 領域拡張（サブメニュー含む）
        mx0, mx1 = best_menu["x"] - 50, screen_w
        my0, my1 = best_menu["y"], best_menu["y"] + best_menu["h"]
        
        # menu-item を探して縦に拡張
        for n in nodes:
            if (n.get("tag") or "").lower() == "menu-item":
                b = node_bbox_from_raw(n)
                if b["x"] > mx0:
                    my0 = min(my0, b["y"])
                    my1 = max(my1, b["y"] + b["h"])

        modal, bg = [], []
        for n in nodes:
            b = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(b)
            if mx0 <= cx <= mx1 and my0 <= cy <= my1:
                modal.append(n)
            else:
                bg.append(n)
        return modal, bg


# ============================================================================
# 3. Chrome Compressor Implementation
# ============================================================================

class ChromeCompressor(BaseA11yCompressor):
    domain_name = "chrome"

    def get_modal_detectors(self) -> List[ModalDetector]:
        return [
            CookieBannerDetector(debug=True),
            CenteredOverlayDetector(debug=True),
            FloatingMenuDetector(),
            FullscreenOverlayDetector(debug=True),
        ]

    def split_static_ui(
        self,
        nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> Tuple[List[Node], List[Node]]:
        """
        モーダル検出に不要な「静的UI」を nodes から抜き出して、
        最後にくっつけるために返す。
        """
        # 1) まず Chrome の領域分類を dry_run で走らせて、
        #    WINDOW_CONTROLS / BROWSER_TABS / BROWSER_UI を特定する
        regions = self.get_semantic_regions(nodes, screen_w, screen_h, dry_run=True)

        forbidden_ids = set()
        for key in ("WINDOW_CONTROLS", "BROWSER_TABS", "BROWSER_UI"):
            for n in regions.get(key, []):
                forbidden_ids.add(id(n))

        LAUNCHER_X_MAX = int(screen_w * 0.035)
        STATUS_Y_MIN = int(screen_h * 0.90)

        nodes_for_modal: List[Node] = []
        static_nodes: List[Node] = []

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            tag = (n.get("role") or n.get("tag") or "").lower()
            name = (n.get("name") or "").strip().lower()

            # ----------------------------------------------------------
            # (A) ブラウザ上部UI (タブ / アドレスバー / 戻る・リロードなど)
            # ----------------------------------------------------------
            if id(n) in forbidden_ids:
                static_nodes.append(n)
                continue

            # ----------------------------------------------------------
            # (B) Ubuntu 左ドックっぽいもの
            # ----------------------------------------------------------
            if x < LAUNCHER_X_MAX:
                if w < screen_w * 0.06 and h < screen_h * 0.12:
                    # ラベルが短い or 無いアイコン → ドックとみなして静的UIへ
                    if not name or len(name) <= 12:
                        static_nodes.append(n)
                        continue

            # ----------------------------------------------------------
            # (C) 画面下部のステータスバー的なもの
            # ----------------------------------------------------------
            if y > STATUS_Y_MIN:
                if tag in ("status-bar", "status"):
                    static_nodes.append(n)
                    continue

            # ----------------------------------------------------------
            # 上記のどれにも該当しないものだけを、モーダル検出に渡す
            # ----------------------------------------------------------
            nodes_for_modal.append(n)

        return nodes_for_modal, static_nodes

    def _estimate_toolbar_y(self, nodes: List[Node], screen_h: int) -> int:
        # 1. まずアンカー（Reload / Address bar / Bookmark 等）だけを見る
        anchor_ys = []

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").strip().lower()

            if tag in ("push-button", "button") and name in BROWSER_UI_ANCHOR_BUTTONS:
                bbox = node_bbox_from_raw(n)
                anchor_ys.append(bbox["y"] + bbox["h"] // 2)
            elif tag in ("entry", "text", "text box") and name in BROWSER_UI_ANCHOR_ENTRIES:
                bbox = node_bbox_from_raw(n)
                anchor_ys.append(bbox["y"] + bbox["h"] // 2)

        if anchor_ys:
            # アンカーが見つかったら、その中央値を toolbar_center_y とする
            return int(median(anchor_ys))

        # 2. アンカーが見つからない場合だけ、従来のキーワードベースにフォールバック
        # 画面上部30%より下にあるものは無視（誤爆防止）
        LIMIT_Y = screen_h * 0.3
        
        # ツールバーによくあるキーワード (小文字)
        TOOLBAR_KEYWORDS = {
            "back", "forward", "reload", "refresh", "home",
            "address", "search", "location",  # アドレスバー
            "extensions", "menu", "settings", "customize" # 右上の機能
        }

        candidates_y = []

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            
            # ボタンや入力欄のみを対象にする
            if tag not in ("push-button", "entry", "toggle-button"):
                continue

            # 画面の下の方にあるものは無視
            bbox = node_bbox_from_raw(n)
            cy = bbox["y"] + bbox["h"] // 2
            if cy > LIMIT_Y:
                continue

            name = (n.get("name") or n.get("text") or "").strip().lower()
            
            # キーワードが含まれていれば候補に追加
            if any(kw in name for kw in TOOLBAR_KEYWORDS):
                candidates_y.append(cy)

        # 候補が見つかればその中央値を返す
        if candidates_y:
            return int(median(candidates_y))
        
        # 見つからなければデフォルト値 (画面上部15%)
        return int(screen_h * 0.15)

    def get_semantic_regions(self, nodes: List[Node], w: int, h: int, dry_run: bool = False) -> Dict[str, List[Node]]:
        regions = {
            "WINDOW_CONTROLS": [], "BROWSER_TABS": [], "BROWSER_UI": [], "CONTENT": [], "APP_LAUNCHER": []
        }
        
        LAUNCHER_X_MAX = int(w * 0.035) 
        ICON_W_MAX = int(w * 0.05)

        # ====================================================================
        # 1. ツールバー中心Yの推定
        # ====================================================================
        toolbar_center_y = self._estimate_toolbar_y(nodes, h)
        
        # 定数定義
        TITLEBAR_H = 60
        TOOLBAR_TOL = min(int(h * 0.03), 30)
        
        # タブ領域の下限設定
        TOOLBAR_HALF_HEIGHT = 25
        TABSTRIP_Y_MIN = int(TITLEBAR_H * 0.7)
        TABSTRIP_Y_MAX = toolbar_center_y - TOOLBAR_HALF_HEIGHT
        
        # ウィンドウ制御ボタン用エリア推定
        win_controls_min_x = w + 1
        TARGET_NAMES = {"close", "minimize", "restore", "minimise", "maximize"}
        candidates = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").strip().lower()
            if tag == "push-button" and name in TARGET_NAMES:
                bbox = node_bbox_from_raw(n)
                if bbox["y"] < TITLEBAR_H:
                    candidates.append(bbox)
        
        if candidates:
            candidates.sort(key=lambda b: b["x"], reverse=True)
            anchor = candidates[0]
            if anchor["x"] > w * 0.8:
                win_controls_min_x = anchor["x"] + anchor["w"] - 200
        else:
            win_controls_min_x = int(w * 0.80)

        # ループ前に一度だけアンカー存在チェック
        has_toolbar_anchors = False
        for n in nodes:
            tag0 = (n.get("tag") or "").lower()
            name0 = (n.get("name") or "").strip().lower()

            if tag0 in ("push-button", "button", "toggle-button") and name0 in BROWSER_UI_ANCHOR_BUTTONS:
                has_toolbar_anchors = True
                break
            if tag0 in ("entry", "text", "text box") and name0 in BROWSER_UI_ANCHOR_ENTRIES:
                has_toolbar_anchors = True
                break


        # ====================================================================
        # 2. メインループ
        # ====================================================================
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            name = (n.get("name") or "").strip()
            text = (n.get("text") or "").strip()
            label = name or text
            
            bbox = node_bbox_from_raw(n)
            y = bbox["y"]
            x = bbox["x"]
            cx, cy = bbox_to_center_tuple(bbox) 

            # ----------------------------------------------------------------
            # Priority 1: Window Controls
            # ----------------------------------------------------------------
            is_titlebar_area = y < h * 0.12  

            if is_titlebar_area and tag == "push-button":
                lower_name = (n.get("name") or "").strip().lower()
                if lower_name in WINDOW_CONTROL_NAMES and x >= win_controls_min_x:
                    if not dry_run:
                        n["tag"] = "window-button"
                    regions["WINDOW_CONTROLS"].append(n)
                    continue

            # ----------------------------------------------------------------
            # Priority 2: Browser UI (Tabsより先に判定)
            # ----------------------------------------------------------------
            lower_name = (n.get("name") or "").strip().lower()

            if tag in ("push-button", "button", "toggle-button") and lower_name in BROWSER_UI_ANCHOR_BUTTONS:
                if not dry_run:
                    n["tag"] = "browser-button"
                regions["BROWSER_UI"].append(n)
                continue

            if tag in ("entry", "text box", "text") and lower_name in BROWSER_UI_ANCHOR_ENTRIES:
                if not dry_run:
                    n["tag"] = "browser-entry"
                regions["BROWSER_UI"].append(n)
                continue

            # 2-2. フォールバック（アンカーが1つも無いときだけ）
            if not has_toolbar_anchors:
                diff_y = abs(cy - toolbar_center_y)
                is_toolbar_area = diff_y <= TOOLBAR_TOL
            
                # ガード1: ショートカットキー
                if "ctrl+" in lower_name:
                    is_toolbar_area = False

                # ガード2: 右側エリアの座標判定
                elif x > w * 0.8:
                    if diff_y > 20: 
                        is_toolbar_area = False
                    elif cy > h * 0.12:
                        is_toolbar_area = False

                # ガード3: セマンティクス
                if "menu" in tag or "menu" in role:
                    is_toolbar_area = False

                # ★追加ガード: Apply などの特定のボタンは絶対にツールバーとみなさない
                if lower_name in ("apply", "change store", "search"):
                     is_toolbar_area = False

                # ---------------------

                if is_toolbar_area:
                    if tag in ("push-button", "entry", "combo-box", "menu-item", "toggle-button"):
                        if not dry_run:
                            if tag == "entry":
                                n["tag"] = "browser-entry"
                            elif tag == "combo-box":
                                n["tag"] = "browser-combo"
                            else:
                                n["tag"] = "browser-button"
                        regions["BROWSER_UI"].append(n)
                    continue

            # ----------------------------------------------------------------
            # Priority 3: Browser Tabs
            # ----------------------------------------------------------------
            lower_name = (n.get("name") or "").strip().lower()
            tab_cy_ok = TABSTRIP_Y_MIN <= cy <= TABSTRIP_Y_MAX
            not_in_win_controls = x < win_controls_min_x

            # ─ 先にアンカー判定 ─
            if lower_name in BROWSER_TAB_ANCHORS and tab_cy_ok and not_in_win_controls:
                if not dry_run:
                    if "tab" in role or tag == "page tab":
                        n["tag"] = "browser-tab"
                    else:
                        n["tag"] = "browser-tab-button"
                regions["BROWSER_TABS"].append(n)
                continue

            # ----------------------------------------------------------------
            # ★ Priority 4: APP_LAUNCHER (Contentの前にチェック)
            # ----------------------------------------------------------------
            if x <= LAUNCHER_X_MAX and bbox["w"] <= ICON_W_MAX and bbox["h"] >= 40:
                if tag in ("push-button", "toggle-button"):
                    if not dry_run: n["tag"] = "launcher-app"
                    regions["APP_LAUNCHER"].append(n)
                    continue # ランチャー要素はコンテンツではない

            # ----------------------------------------------------------------
            # Priority 5: Content
            # ----------------------------------------------------------------
            if not label: continue
            if len(label) == 1 and not label.isalnum(): continue
            if label in ("ADVERTISEMENT",): continue

            # Heading判定の修正
            # staticかつrole=headingなら無条件でHeading
            if tag == "static" and role == "heading":
                if not dry_run: n["tag"] = "heading"
            
            # ヒューリスティック判定の厳格化
            # 修正前: len(label) > 10
            # 修正後: 10 < len(label) < 60  (長すぎるものは説明文とみなす)
            #elif tag == "static" and 10 < len(label) < 60 and label[0].isupper() and not label.endswith("."):
                # さらにガード: 改行が含まれていたらHeadingではない可能性が高い
            #    if "\n" not in label:
            #        if not dry_run: n["tag"] = "heading"

            if tag == "list-item" and "result" in label.lower():
                if not dry_run: n["tag"] = "static"

            regions["CONTENT"].append(n)

        if regions["CONTENT"]:
            regions["CONTENT"] = self._dedup_overlapping_content(regions["CONTENT"])

        return regions

    def get_meta_header(self, regions: Dict[str, List[Node]]) -> List[str]:
        raw_url = ""
        for n in regions.get("BROWSER_UI", []):
            # ★ 修正: tag名が書き換わっているので browser-entry もチェック
            if "address" in (n.get("name") or "").lower() and n.get("tag") in ("entry", "browser-entry"):
                raw_url = n.get("text") or ""
                break
        if not raw_url: return []
        return [f"URL: {self._format_url(raw_url)}"]

    def _format_url(self, raw_url):
        tmp = raw_url.strip()
        # ★ 改善: スキームなしURL対応
        if "://" not in tmp:
            tmp = "https://" + tmp
            
        try:
            p = urlparse(tmp)
            if "google" in p.netloc and p.path.startswith("/search"):
                qs = parse_qs(p.query)
                q = qs.get("q", [""])[0]
                if q: return f'Google Search: "{unquote(q).replace("+", " ")}"'
            
            short = p.netloc + p.path
            return short if len(short) < 80 else short[:77] + "..."
        except: return raw_url

    def process_content_lines(self, nodes: List[Node], screen_w: int, screen_h: int) -> List[str]:
        """
        コンテンツ領域専用の圧縮処理。
        ここでゴミ除去 (_should_skip_for_content) を行う。
        """
        # 1. フィルタリング (ここで実施)
        filtered_nodes = [n for n in nodes if not self._should_skip_for_content(n)]
        
        # 2. タプル化 (Baseクラスのメソッドを利用)
        tuples = self._nodes_to_tuples(filtered_nodes)
        tuples.sort()
        
        y_tol = int(screen_h * 0.03)
        x_tol = int(screen_w * 0.15)
        tuples = merge_fragmented_static_lines(tuples, y_tol, x_tol)
        
        return build_hierarchical_content_lines(
            tuples,
            big_gap_px=None,              # 自動
            heading_section_gap_px=None,  # 自動
        )


    def _dedup_overlapping_content(self, nodes: List[Node]) -> List[Node]:
        """
        CONTENT 内のノードについて、
        - label が同じ
        かつ
        - y 座標（行）が近い
        ノード群から「より操作に関係あるノード」だけを残し、それ以外を削除する。

        ※ x 方向は問わず、「同じ行に同じテキストが並んでいる」ものも1つにまとめる。
        """
        from collections import defaultdict

        if not nodes:
            return nodes

        # 「同じ行」とみなす y の差（ピクセル）
        Y_TOL = 20  # 13px 差の "All" も同一行として入るようにしておく

        # tag ごとの優先度（小さいほど優先）
        TAG_PRIORITY = {
            "entry": 0,
            "combo-box": 0,
            "check-box": 0,
            "radio-button": 0,
            "toggle-button": 0,
            "spin-button": 0,
            "slider": 0,

            "push-button": 1,
            "menu-item": 2,

            "link": 3,

            "heading": 4,

            "image": 5,

            "label": 6,
            "static": 7,
            "section": 8,
            "paragraph": 8,
        }

        # ラベルごとに index と座標を集める
        label_groups = defaultdict(list)  # (block, label) -> [ (idx, cx, cy) ]
        centers = {}

        current_block = None

        for idx, n in enumerate(nodes):
            # BLOCKヘッダの表現に合わせてここは調整
            if n.get("kind") == "block_header":
                current_block = (n.get("name") or "").strip()
                continue

            name = (n.get("name") or "").strip()
            text = (n.get("text") or "").strip()
            label = name or text
            if not label:
                continue

            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            centers[idx] = (cx, cy)

            key = (current_block, label.lower())
            label_groups[key].append((idx, cx, cy))

        to_drop = set()

        for key, items in label_groups.items():
            if len(items) <= 1:
                continue

            # その BLOCK + ラベル内で、「同じ行（yが近い）」ごとにクラスタにまとめる
            items.sort(key=lambda t: (t[2], t[1]))  # cy, cx
            clusters = []
            current = [items[0]]
            for i in range(1, len(items)):
                idx_i, cx_i, cy_i = items[i]
                idx_p, cx_p, cy_p = current[-1]

                # ★ x は見ずに、y だけで「同じ行」判定
                if abs(cy_i - cy_p) <= Y_TOL:
                    current.append(items[i])
                else:
                    clusters.append(current)
                    current = [items[i]]
            clusters.append(current)

            for cluster in clusters:
                if len(cluster) <= 1:
                    continue

                # 最も「操作として意味がある」ノードを残す
                best_idx = None
                best_score = None
                for idx_i, cx_i, cy_i in cluster:
                    tag = (nodes[idx_i].get("tag") or "").lower()
                    score = TAG_PRIORITY.get(tag, 100)
                    if best_score is None or score < best_score:
                        best_score = score
                        best_idx = idx_i

                # その他は drop
                for idx_i, _, _ in cluster:
                    if idx_i != best_idx:
                        to_drop.add(idx_i)

        return [n for i, n in enumerate(nodes) if i not in to_drop]

    def _should_skip_for_content(self, node: Node) -> bool:
        # 既存のロジック
        tag = (node.get("tag") or "").lower()
        name = (node.get("name") or "").strip()
        text = (node.get("text") or "").strip()
        label = name or text

        if not label: return True
        if len(label) == 1 and not label.isalnum(): return True
        
        lower = label.lower()
        if ("http" in lower or "https" in lower) and " " not in label and len(label) > 30:
            if tag not in ("link", "push-button"):
                return True
        return False