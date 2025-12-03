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
    画面下部の Cookie 同意バナーを検出する。
    """
    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        FOOTER_START_Y = int(screen_h * 0.65)
        
        anchor_indices = []
        cookie_related_indices = set()
        all_centers = []

        # 1. アンカー探索
        for idx, n in enumerate(nodes):
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            all_centers.append((cx, cy))

            if cy < FOOTER_START_Y: continue

            tag = (n.get("tag") or "").lower()
            label = (n.get("name") or n.get("text") or "").strip()
            lower = label.lower()

            is_anchor = False
            is_related = False

            if tag in ("push-button", "link") and label in COOKIE_BUTTON_ANCHORS:
                is_anchor = True
                is_related = True
            
            if any(kw in lower for kw in COOKIE_TEXT_KEYWORDS):
                is_related = True
                if tag in ("push-button", "link"):
                    is_anchor = True

            if is_anchor: anchor_indices.append(idx)
            if is_related: cookie_related_indices.add(idx)

        if not anchor_indices or not cookie_related_indices:
            return [], nodes

        # 2. バウンディングボックス計算
        min_cx = min(all_centers[i][0] for i in cookie_related_indices)
        max_cx = max(all_centers[i][0] for i in cookie_related_indices)
        min_cy = min(all_centers[i][1] for i in cookie_related_indices)
        max_cy = max(all_centers[i][1] for i in cookie_related_indices)

        MARGIN_X = int(screen_w * 0.08)
        MARGIN_Y = 40
        box_l, box_r = min_cx - MARGIN_X, max_cx + MARGIN_X
        box_t, box_b = min_cy - MARGIN_Y, max_cy + MARGIN_Y

        # 3. 分割
        modal, bg = [], []
        for idx, n in enumerate(nodes):
            cx, cy = all_centers[idx]
            # BBox内 かつ (関連ノード or Closeボタン)
            is_in_box = box_l <= cx <= box_r and box_t <= cy <= box_b
            tag = (n.get("tag") or "").lower()
            label = (n.get("name") or n.get("text") or "").strip().lower()
            
            is_target = idx in cookie_related_indices or (tag == "push-button" and label == "close")

            if is_in_box and is_target:
                modal.append(n)
            else:
                bg.append(n)
        
        return modal, bg


class FullscreenOverlayDetector(ModalDetector):
    """
    Delta航空のような全画面オーバーレイ（Close Dialog ... Confirm）を検出。
    """
    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        TOP_MIN_Y = int(screen_h * 0.08)
        TOP_MAX_Y = int(screen_h * 0.55)
        BOT_MIN_Y = int(screen_h * 0.50)
        
        TOP_ANCHORS = ["close dialog", "close"]
        BOT_ANCHORS = ["confirm my choices", "accept all", "save preferences"]
        
        top_centers, bot_centers = [], []

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag != "push-button": continue
            label = (n.get("name") or n.get("text") or "").strip().lower()
            if not label: continue

            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            if cx < screen_w * 0.05: continue # 左端除外

            # Top Anchor
            if TOP_MIN_Y <= cy <= TOP_MAX_Y:
                if any(k in label for k in TOP_ANCHORS) and cy > screen_h * 0.1:
                    top_centers.append(cy)

            # Bottom Anchor
            if cy >= BOT_MIN_Y:
                if any(k in label for k in BOT_ANCHORS):
                    bot_centers.append(cy)

        if not top_centers or not bot_centers:
            return [], nodes

        # 範囲決定
        top_y = min(top_centers) - 40
        bot_y = max(bot_centers) + 40
        
        if (bot_y - top_y) < screen_h * 0.4: # 小さすぎる場合は対象外
            return [], nodes

        modal, bg = [], []
        for n in nodes:
            bbox = node_bbox_from_raw(n)
            _, cy = bbox_to_center_tuple(bbox)
            if top_y <= cy <= bot_y:
                modal.append(n)
            else:
                bg.append(n)
        return modal, bg


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
            CookieBannerDetector(),
            FloatingMenuDetector(),
            FullscreenOverlayDetector(),
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
            "WINDOW_CONTROLS": [], "BROWSER_TABS": [], "BROWSER_UI": [], "CONTENT": [],
        }
        
        # ====================================================================
        # 1. ツールバー中心Yの推定
        # ====================================================================
        toolbar_center_y = self._estimate_toolbar_y(nodes, h)
        
        # 定数定義
        TITLEBAR_H = 60
        TOOLBAR_TOL = int(h * 0.03)
        
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
            # Priority 4: Content
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