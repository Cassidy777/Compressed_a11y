import re
from typing import List, Dict, Any, Tuple, Optional, Set
from urllib.parse import urlparse, parse_qs, unquote
from statistics import median

from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    merge_fragmented_static_lines, build_hierarchical_content_lines
)
from ..core.modal_strategies import ( ModalDetector, DiffModalDetector )


# ============================================================================
# 1. Chrome-Specific Constants
# ============================================================================

# クッキー/バナー判定用の安全なキーワード（文脈依存しないもの）
SAFE_BANNER_KEYWORDS = {
    "cookie", "cookies", "privacy", "adopt", "reject", "consent", "gdpr",
    "クッキー", "プライバシー", "同意"
}

# 構造スコア判定用のインタラクティブ要素
FORM_INTERACTIVE_TAGS = {
    "entry", "input", "text field", "textarea"
}
ACTION_BUTTON_TAGS = {
    "push-button", "button", "submit", "menu-item"
}
TOGGLE_TAGS = {
    "toggle-button", "check-box", "radio-button", "switch"
}

# ブラウザUI判定用（既存維持）
BROWSER_UI_ANCHOR_BUTTONS = {
    "reload", "you", "chrome", "bookmark this tab", "back",
    "view site information", "extensions", "side panel",
}
BROWSER_UI_ANCHOR_ENTRIES = {"address and search bar"}
BROWSER_TAB_ANCHORS = {"search tabs", "new tab", "close"}
WINDOW_CONTROL_NAMES = {"minimise", "minimize", "restore", "maximize", "close"}


# ============================================================================
# 2. Hybrid Modal Detector (Phase 1 + Phase 2)
# ============================================================================

class HybridModalDetector(ModalDetector):
    """
    【案2+案3のハイブリッド実装】
    Phase 1: 画面上下の「エッジバナー（Cookie通知など）」を幾何学的特徴で検出・確保する。
    Phase 2: 残りの領域から、スコアベースで「中央ポップアップ」を検出する。
    
    これにより、Cars.comのような「下部バナー」と「中央ポップアップ」が共存するケースや、
    Delta/Drugsのような「キーワードに頼らない構造的モーダル」に対応する。
    """

    def __init__(self, debug: bool = False):
        self.debug = debug

    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        if not nodes:
            return [], nodes

        if self.debug:
            print(f"\n=== HybridModalDetector (w={screen_w}, h={screen_h}) ===")

        # 全ノードのインデックス集合
        all_indices = set(range(len(nodes)))
        
        # --- [Phase 1] Edge Banner Detection (案3: ボトム分離) ---
        # 画面下部/上部に張り付いている横長のバナーを先に特定する
        banner_indices = self._detect_edge_banners(nodes, screen_w, screen_h)
        
        if self.debug and banner_indices:
            print(f"[Phase 1] Detected Banner Nodes: {len(banner_indices)}")

        # Phase 2の対象は、バナーとして検出されなかったノード群
        remaining_indices = list(all_indices - banner_indices)
        
        # --- [Phase 2] Centered Structure Scoring (案2: 構造スコア) ---
        # 残ったノードから、中央に密集し、かつフォームやトグルなどの構造を持つ塊を探す
        popup_indices = self._detect_centered_popup(nodes, remaining_indices, screen_w, screen_h)

        if self.debug and popup_indices:
            print(f"[Phase 2] Detected Popup Nodes: {len(popup_indices)}")

        # 最終的なモーダル集合 = バナー + ポップアップ
        final_modal_indices = banner_indices | popup_indices

        modal_nodes = []
        bg_nodes = []

        for i, n in enumerate(nodes):
            if i in final_modal_indices:
                modal_nodes.append(n)
            else:
                bg_nodes.append(n)

        return modal_nodes, bg_nodes

    def _detect_edge_banners(self, nodes: List[Node], sw: int, sh: int) -> Set[int]:
        """
        画面下端または上端に吸着している「横長」の領域を検出する。
        """
        candidates = set()
        
        # しきい値設定
        BOTTOM_THRESH_Y = int(sh * 0.75)  # これより下ならボトムバナー候補
        TOP_THRESH_Y = int(sh * 0.15)     # これより上かつ...
        ASPECT_RATIO_MIN = 2.5            # 横長であること (w/h)
        
        # 簡易クラスタリング用のビン
        bottom_nodes = []
        
        for i, n in enumerate(nodes):
            bbox = node_bbox_from_raw(n)
            y = bbox["y"]
            h = bbox["h"]
            cy = y + h / 2
            
            # ボトム判定
            if cy > BOTTOM_THRESH_Y:
                bottom_nodes.append(i)
        
        if not bottom_nodes:
            return set()

        # ボトム領域のノード群が「バナー的」か判定
        # 1. role="alert" や "section" を含むか
        # 2. キーワード (cookie, privacy) があるか
        # 3. Close/Accept ボタンがあるか
        
        has_banner_feature = False
        min_x, max_x = sw, 0
        min_y, max_y = sh, 0
        
        relevant_indices = set()

        for i in bottom_nodes:
            n = nodes[i]
            bbox = node_bbox_from_raw(n)
            tag = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            label = (n.get("name") or n.get("text") or "").lower()
            
            # ジオメトリ更新
            min_x = min(min_x, bbox["x"])
            max_x = max(max_x, bbox["x"] + bbox["w"])
            min_y = min(min_y, bbox["y"])
            max_y = max(max_y, bbox["y"] + bbox["h"])

            # 特徴チェック
            if role in ("alert", "banner"):
                has_banner_feature = True
            if any(kw in label for kw in SAFE_BANNER_KEYWORDS):
                has_banner_feature = True
            if tag in ACTION_BUTTON_TAGS and any(w in label for w in ["accept", "reject", "close", "agree", "×"]):
                has_banner_feature = True
            
            relevant_indices.add(i)

        if not relevant_indices:
            return set()

        width = max_x - min_x
        height = max_y - min_y
        if height < 10: return set()
        
        aspect = width / height
        
        # 横長であり、かつバナー特徴がある場合のみ採用
        if aspect > ASPECT_RATIO_MIN and has_banner_feature:
            if self.debug:
                print(f"  -> Edge Banner Found: y={min_y}~{max_y}, aspect={aspect:.2f}")
            return relevant_indices
        
        return set()

    def _detect_centered_popup(self, nodes: List[Node], candidate_indices: List[int], sw: int, sh: int) -> Set[int]:
        """
        案2: スコアリングによる中央ポップアップ検出 (修正版: ヘッダー誤爆対策入り)
        """
        if not candidate_indices:
            return set()

        # 1. 簡易クラスタリング
        centers = []
        for i in candidate_indices:
            bbox = node_bbox_from_raw(nodes[i])
            cx, cy = bbox["x"] + bbox["w"] // 2, bbox["y"] + bbox["h"] // 2
            centers.append((i, cx, cy))

        DIST_THRESH = min(sw, sh) * 0.15
        clusters = []
        visited = set()

        for i in range(len(centers)):
            idx1, cx1, cy1 = centers[i]
            if idx1 in visited: continue
            
            group = [idx1]
            visited.add(idx1)
            queue = [i]
            
            while queue:
                curr = queue.pop(0)
                curr_cx, curr_cy = centers[curr][1], centers[curr][2]
                
                for j in range(len(centers)):
                    idx2, cx2, cy2 = centers[j]
                    if idx2 in visited: continue
                    
                    dist = ((curr_cx - cx2)**2 + (curr_cy - cy2)**2)**0.5
                    if dist < DIST_THRESH:
                        visited.add(idx2)
                        group.append(idx2)
                        queue.append(j)
            clusters.append(group)

        # 2. クラスタごとのスコアリング
        best_cluster = set()
        max_score = 0.0
        SCORE_THRESHOLD = 65.0

        screen_cx, screen_cy = sw // 2, sh // 2
        
        # ★ ヘッダー誤爆防止用の定数
        HEADER_Y_LIMIT = sh * 0.25      # 画面上部25%より上から始まるものはヘッダーの疑い
        WIDE_HEADER_RATIO = 0.8         # 画面幅の80%以上を使うものはヘッダー/フッターの疑い
        FLAT_ASPECT_RATIO = 4.0         # 横:縦比が4:1以上の「細長い帯」はモーダルではない

        CHROME_NTP_KEYWORDS = {"search google or type a url", "web store"}

        for group in clusters:
            xs, ys = [], []
            structure_score = 0
            
            inputs = 0
            toggles = 0
            buttons = 0
            has_close = False
            has_ntp_keyword = False
            
            group_indices = set(group)
            
            for idx in group:
                n = nodes[idx]
                bbox = node_bbox_from_raw(n)
                xs.extend([bbox["x"], bbox["x"] + bbox["w"]])
                ys.extend([bbox["y"], bbox["y"] + bbox["h"]])
                
                tag = (n.get("tag") or "").lower()
                label = (n.get("name") or n.get("text") or "").lower()

                if any(k in label for k in CHROME_NTP_KEYWORDS):
                    has_ntp_keyword = True
                
                if tag in FORM_INTERACTIVE_TAGS:
                    inputs += 1
                elif tag in TOGGLE_TAGS:
                    toggles += 1
                elif tag in ACTION_BUTTON_TAGS:
                    buttons += 1
                    if "close" in label or label == "×" or label == "x":
                        has_close = True
                
                if tag == "section" and "consent" in label:
                    structure_score += 10

            if not xs: continue
            
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            w, h = max_x - min_x, max_y - min_y
            cx, cy = min_x + w / 2, min_y + h / 2
            
            aspect_ratio = w / (h + 1e-9)
            width_ratio = w / (sw + 1e-9)

            # === [GUARD] ヘッダー/ナビゲーションバー除外ロジック ===
            is_rejected = False
            
            # 1. 位置と形状によるガード
            # 「画面上部(y < 25%)から始まり」かつ「横幅が広い(>80%)」または「極端に横長(アスペクト比>4)」
            if min_y < HEADER_Y_LIMIT:
                if width_ratio > WIDE_HEADER_RATIO:
                    is_rejected = True # ヘッダーバーと判定
                elif aspect_ratio > FLAT_ASPECT_RATIO:
                    is_rejected = True # ナビゲーション帯と判定

            # 2. 検索バー誤爆対策
            # 入力フォームがあるが、縦幅が極端に狭い (Search Bar only) 場合はモーダルとしない
            if inputs > 0 and h < sh * 0.1:
                is_rejected = True

            if has_ntp_keyword:
                is_rejected = True

            if is_rejected:
                if self.debug:
                    print(f"  [Cluster REJECTED] Header/Nav detected: y={min_y}, w_ratio={width_ratio:.2f}, aspect={aspect_ratio:.2f}")
                continue
            # ====================================================

            # --- 構造点 (Structure Score) ---
            # Pattern A: 入力フォーム (Drugs.com対策)
            if inputs >= 1 and buttons >= 1:
                structure_score += 40
            
            # Pattern B: 設定/同意ダイアログ (Delta対策)
            if toggles >= 2 and buttons >= 1:
                structure_score += 40
            
            # Pattern C: 単純な通知
            if inputs == 0 and buttons >= 1 and len(group) > 3:
                structure_score += 20
            
            if has_close:
                structure_score += 10

            # --- 基本点 (Base Score) ---
            dist_norm = ((cx - screen_cx)**2 + (cy - screen_cy)**2)**0.5 / (sh * 0.5)
            center_score = max(0, 30 * (1.0 - dist_norm))
            
            density_score = min(len(group), 20)
            isolation_score = 10 

            total_score = structure_score + center_score + density_score + isolation_score
            
            if self.debug:
                print(f"  [Cluster] score={total_score:.1f} (Struct={structure_score}, Center={center_score:.1f}) | Inputs={inputs}, Toggles={toggles}, Y={min_y}")

            if total_score > max_score:
                max_score = total_score
                best_cluster = group_indices

        if max_score > SCORE_THRESHOLD:
            final_set = set(best_cluster)
            
            # 吸収処理
            xs, ys = [], []
            for idx in best_cluster:
                bbox = node_bbox_from_raw(nodes[idx])
                xs.extend([bbox["x"], bbox["x"] + bbox["w"]])
                ys.extend([bbox["y"], bbox["y"] + bbox["h"]])
            
            bx1, bx2 = min(xs) - 20, max(xs) + 20
            by1, by2 = min(ys) - 20, max(ys) + 20
            
            for i in candidate_indices:
                if i in final_set: continue
                bbox = node_bbox_from_raw(nodes[i])
                cx = bbox["x"] + bbox["w"] // 2
                cy = bbox["y"] + bbox["h"] // 2
                
                if bx1 <= cx <= bx2 and by1 <= cy <= by2:
                    final_set.add(i)
            
            return final_set

        return set()


# ============================================================================
# 3. Floating / Fullscreen Detectors (Keep or Update lightly)
# ============================================================================

class FloatingMenuDetector(ModalDetector):
    """右上のメニュー等を検出 (既存ロジック維持)"""
    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        candidates = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            bbox = node_bbox_from_raw(n)
            # 画面右半分にある menu / menu-item
            if (tag == "menu" or role == "menu") and bbox["x"] > screen_w * 0.4:
                candidates.append(bbox)
        
        if not candidates:
            return [], nodes
            
        best_menu = max(candidates, key=lambda b: b["w"] * b["h"])
        mx0, mx1 = best_menu["x"] - 50, screen_w
        my0, my1 = best_menu["y"], best_menu["y"] + best_menu["h"]
        
        # 配下のitemまで拡張
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


class FullscreenOverlayDetector(ModalDetector):
    """
    Delta航空のような全画面オーバーレイを検出。
    HybridDetectorで漏れた場合の保険として機能させる。
    """
    def __init__(self, debug: bool = False):
        self.debug = debug

    def detect(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        # 簡易実装: CloseボタンとConfirm系のボタンが離れて存在する場合
        top_close = False
        bottom_confirm = False
        
        # 上部エリア(Top 20%)と下部エリア(Bottom 20%)の走査
        for n in nodes:
            label = (n.get("name") or n.get("text") or "").lower()
            tag = (n.get("tag") or "").lower()
            bbox = node_bbox_from_raw(n)
            cy = bbox["y"] + bbox["h"] // 2
            
            if cy < screen_h * 0.2:
                if "close" in label or label in ("×", "x"):
                    top_close = True
            elif cy > screen_h * 0.8:
                if "confirm" in label or "agree" in label or "accept" in label:
                    if tag in ACTION_BUTTON_TAGS:
                        bottom_confirm = True
        
        if top_close and bottom_confirm:
            # 全画面とみなして、明らかにUIでないものを全てモーダルとする
            # (ここでは厳密な切り分けが難しいため、HybridDetectorを優先し、ここは空を返すか
            #  あるいは非常に保守的な動作に留める)
            pass
            
        return [], nodes


# ============================================================================
# 4. Chrome Compressor Implementation
# ============================================================================

class ChromeCompressor(BaseA11yCompressor):
    domain_name = "chrome"

    enable_multiline_normalization = False
    enable_static_line_merge = False


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._prev_url_sig: Optional[str] = None

    # -----------------------------
    # URL-based transition guard
    # -----------------------------
    def _extract_raw_url_from_nodes(self, nodes: List[Node]) -> str:
        """
        アドレスバーのテキストを取得する。
        タグ判定を緩め("text field"等も許容)、確実にURLを拾えるようにする。
        """
        TARGET_TAGS = {"entry", "browser-entry", "text field", "text"}
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").lower()
            
            # タグが対象で、かつ名前に 'address' が含まれるか、定義済みUIリストにあるか
            if tag in TARGET_TAGS:
                if (name in BROWSER_UI_ANCHOR_ENTRIES) or ("address" in name):
                    return (n.get("text") or "").strip()
        return ""

    def _url_signature(self, raw_url: str) -> str:
        """
        比較用に正規化したURLシグネチャを作る。
        - schemeが無ければ足す
        - 基本は netloc + path
        - （必要なら query まで含めたい場合はここを拡張）
        """
        s = (raw_url or "").strip()
        if not s:
            return ""
        if "://" not in s:
            s = "https://" + s
        try:
            p = urlparse(s)
            netloc = (p.netloc or "").lower()
            path = p.path or ""
            return netloc + path
        except Exception:
            return s

    def _reset_prev_base_cache(self) -> None:
        """
        engine側の実装詳細に依存せず、安全に prev_base 系を潰す。
        """
        candidates = [
            "_prev_base", "prev_base",
            "_prev_nodes", "prev_nodes",
            "_prev_base_nodes", "prev_base_nodes",
            "_prev_base_for_diff", "prev_base_for_diff",
        ]
        for attr in candidates:
            if hasattr(self, attr):
                try:
                    setattr(self, attr, None)
                except Exception:
                    pass

    def compress(self, nodes: List[Node], screen_w: int, screen_h: int, *args, **kwargs):
        """
        URLが変わったら「ページ遷移」とみなして prev_base を破棄する。
        """
        raw_url = self._extract_raw_url_from_nodes(nodes)
        curr_sig = self._url_signature(raw_url)

        # URL変化検知
        if self._prev_url_sig and curr_sig and (curr_sig != self._prev_url_sig):
            # 1) compressor側のキャッシュを消す
            self._reset_prev_base_cache()

            # 2) ★ 修正箇所: グローバルキャッシュを消去するために reset() を呼ぶ
            if hasattr(self, 'diff_detector'):
                self.diff_detector.reset()
            else:
                # 万が一未定義なら作る（念のため）
                self.diff_detector = DiffModalDetector()
            
            # 3) 親クラスに渡る prev_nodes 引数も消去しておく（念のため）
            if 'prev_nodes' in kwargs:
                kwargs['prev_nodes'] = None

        # 次回比較用に更新
        if curr_sig:
            self._prev_url_sig = curr_sig

        return super().compress(nodes, screen_w, screen_h, *args, **kwargs)


    def get_modal_detectors(self) -> List[ModalDetector]:
        return [
            # 1. 統合型検出器 (バナーと中央ポップアップを同時に処理可能)
            HybridModalDetector(debug=True),
            
            # 2. 右上メニュー (補完)
            FloatingMenuDetector(),
            
            # 3. 全画面オーバーレイ (補完)
            FullscreenOverlayDetector(debug=False),
        ]

    def detect_modal(self, nodes: List[Node], prev_nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        """
        BaseのDiff検知を実行した後、結果が「巨大すぎる」場合は
        HybridModalDetectorを使って『真のモーダル』だけを救出する。
        """
        # 1. 親クラスのロジック（DiffModalDetector含む）を実行
        modal, bg = super().detect_modal(nodes, prev_nodes, screen_w, screen_h)
        
        if not modal:
            return modal, bg

        # 2. ガード処理: モーダル領域が全ノードの 50% を超える場合、
        #    それは「ポップアップ」ではなく「ページ遷移」である可能性が高い。
        n_ratio = len(modal) / (len(nodes) + 1e-9)
        
        if n_ratio > 0.5:
            # 「巨大モーダル」と認定されたノード群の中から、
            # HybridModalDetector (構造/スコア判定) に合格するものだけを探す。
            # ※ debug=False にしてログを抑制
            refiner = HybridModalDetector(debug=False)
            refined_modal, _ = refiner.detect(modal, screen_w, screen_h)
            
            if refined_modal:
                # 本物のポップアップ（例：Feedbackダイアログ）が見つかった場合
                # -> 見つかったものだけを Modal とし、残りの Diff ノードは背景に戻す
                real_modal_ids = {id(n) for n in refined_modal}
                
                # 背景 = 元の背景 + (Diffで検出されたがPopupではなかったノード)
                final_bg = bg + [n for n in modal if id(n) not in real_modal_ids]
                
                # ノード順序を元のリスト順に整列（念のため）
                # final_bg.sort(key=lambda n: nodes.index(n) if n in nodes else -1) # 必須ではないがあれば安全
                
                return refined_modal, final_bg
            else:
                # ポップアップらしい構造が見つからなかった場合
                # -> 単なるページ遷移とみなし、モーダルなし（全て背景）とする
                return [], nodes

        return modal, bg

    def split_static_ui(self, nodes: List[Node], screen_w: int, screen_h: int) -> Tuple[List[Node], List[Node]]:
        """
        UI分離ロジック
        """
        # dry_run=True で領域判定だけ行う
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

            # 判定済みUIを除外
            if id(n) in forbidden_ids:
                static_nodes.append(n)
                continue

            # 左ドック (Ubuntu)
            if x < LAUNCHER_X_MAX and w < screen_w * 0.06 and h < screen_h * 0.12:
                if not name or len(name) <= 12:
                    static_nodes.append(n)
                    continue

            # 下部ステータスバー
            if y > STATUS_Y_MIN and tag in ("status-bar", "status"):
                static_nodes.append(n)
                continue

            nodes_for_modal.append(n)

        return nodes_for_modal, static_nodes

    # ========================================================================
    # ★ ここから下: 不足していたヘルパーメソッドの復元
    # ========================================================================

    def _estimate_toolbar_y(self, nodes: List[Node], screen_h: int) -> int:
        """
        ツールバー（アドレスバーや戻るボタンがある帯）の中心Y座標を推定する。
        """
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
            return int(median(anchor_ys))

        # 2. アンカーが見つからない場合のフォールバック
        LIMIT_Y = screen_h * 0.3
        TOOLBAR_KEYWORDS = {
            "back", "forward", "reload", "refresh", "home",
            "address", "search", "location", 
            "extensions", "menu", "settings", "customize"
        }

        candidates_y = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag not in ("push-button", "entry", "toggle-button"):
                continue

            bbox = node_bbox_from_raw(n)
            cy = bbox["y"] + bbox["h"] // 2
            if cy > LIMIT_Y:
                continue

            name = (n.get("name") or n.get("text") or "").strip().lower()
            if any(kw in name for kw in TOOLBAR_KEYWORDS):
                candidates_y.append(cy)

        if candidates_y:
            return int(median(candidates_y))
        
        return int(screen_h * 0.15)

    def _should_skip_for_content(self, node: Node) -> bool:
        """コンテンツ処理時にスキップすべきノードか判定"""
        tag = (node.get("tag") or "").lower()
        name = (node.get("name") or "").strip()
        text = (node.get("text") or "").strip()
        label = name or text

        # 空は落とす
        if not label:
            return True

        lower = label.lower()

        # 記号1文字だけは落とす（例： など）
        if len(label) == 1 and not label.isalnum():
            return True

        # 右下の "Home" は Chrome ではノイズになりがちなので落とす
        # ※ cy を計算する
        if self.domain_name == "chrome" and tag == "label" and lower == "home":
            bbox = node_bbox_from_raw(node)
            cy = bbox["y"] + bbox["h"] // 2
            if cy >= int(1080 * 0.90):  # screen_h を渡せない設計なら暫定で1080固定
                return True

        # 長いURLっぽいもの（スペース無し＆長い＆http含む）は落とす
        if ("http" in lower or "https" in lower) and " " not in label and len(label) > 30:
            if tag not in ("link", "push-button"):
                return True

        return False

    def _dedup_overlapping_content(self, nodes: List[Node]) -> List[Node]:
        """重複・冗長なコンテンツノードを間引く"""
        from collections import defaultdict
        if not nodes: return nodes

        Y_TOL = 20
        TAG_PRIORITY = {
            "entry": 0, "combo-box": 0, "check-box": 0, "radio-button": 0,
            "toggle-button": 0, "spin-button": 0, "slider": 0,
            "push-button": 1, "menu-item": 2, "link": 3, "heading": 4,
            "image": 5, "label": 6, "static": 7, "section": 8, "paragraph": 8,
        }

        label_groups = defaultdict(list)
        current_block = None

        for idx, n in enumerate(nodes):
            if n.get("kind") == "block_header":
                current_block = (n.get("name") or "").strip()
                continue

            name = (n.get("name") or "").strip()
            text = (n.get("text") or "").strip()
            label = name or text
            if not label: continue

            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            key = (current_block, label.lower())
            label_groups[key].append((idx, cx, cy))

        to_drop = set()

        for key, items in label_groups.items():
            if len(items) <= 1: continue

            items.sort(key=lambda t: (t[2], t[1]))
            clusters = []
            current = [items[0]]
            
            for i in range(1, len(items)):
                idx_i, cx_i, cy_i = items[i]
                idx_p, cx_p, cy_p = current[-1]
                if abs(cy_i - cy_p) <= Y_TOL:
                    current.append(items[i])
                else:
                    clusters.append(current)
                    current = [items[i]]
            clusters.append(current)

            for cluster in clusters:
                if len(cluster) <= 1: continue
                
                best_idx = None
                best_score = None
                for idx_i, _, _ in cluster:
                    tag = (nodes[idx_i].get("tag") or "").lower()
                    score = TAG_PRIORITY.get(tag, 100)
                    if best_score is None or score < best_score:
                        best_score = score
                        best_idx = idx_i
                
                for idx_i, _, _ in cluster:
                    if idx_i != best_idx:
                        to_drop.add(idx_i)

        return [n for i, n in enumerate(nodes) if i not in to_drop]

    def get_semantic_regions(self, nodes: List[Node], w: int, h: int, dry_run: bool = False) -> Dict[str, List[Node]]:
        """
        ウィンドウ制御、ツールバー、タブ、コンテンツなどの領域意味解析を行う
        """
        regions = {
            "WINDOW_CONTROLS": [], "BROWSER_TABS": [], "BROWSER_UI": [], "CONTENT": [], "APP_LAUNCHER": []
        }
        
        LAUNCHER_X_MAX = int(w * 0.035) 
        ICON_W_MAX = int(w * 0.05)

        # 1. ツールバー中心Yの推定
        toolbar_center_y = self._estimate_toolbar_y(nodes, h)
        
        TITLEBAR_H = 60
        TOOLBAR_TOL = min(int(h * 0.03), 30)
        TOOLBAR_HALF_HEIGHT = 25
        TABSTRIP_Y_MIN = int(TITLEBAR_H * 0.7)
        TABSTRIP_Y_MAX = toolbar_center_y - TOOLBAR_HALF_HEIGHT
        
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

        # 2. メインループ
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

            # Priority 1: Window Controls
            is_titlebar_area = y < h * 0.12  
            if is_titlebar_area and tag == "push-button":
                lower_name = (n.get("name") or "").strip().lower()
                if lower_name in WINDOW_CONTROL_NAMES and x >= win_controls_min_x:
                    if not dry_run: n["tag"] = "window-button"
                    regions["WINDOW_CONTROLS"].append(n)
                    continue

            # Priority 2: Browser UI
            lower_name = (n.get("name") or "").strip().lower()

            if tag in ("push-button", "button", "toggle-button") and lower_name in BROWSER_UI_ANCHOR_BUTTONS:
                if not dry_run: n["tag"] = "browser-button"
                regions["BROWSER_UI"].append(n)
                continue

            if tag in ("entry", "text box", "text") and lower_name in BROWSER_UI_ANCHOR_ENTRIES:
                if not dry_run: n["tag"] = "browser-entry"
                regions["BROWSER_UI"].append(n)
                continue

            if not has_toolbar_anchors:
                diff_y = abs(cy - toolbar_center_y)
                is_toolbar_area = diff_y <= TOOLBAR_TOL
                if "ctrl+" in lower_name: is_toolbar_area = False
                elif x > w * 0.8:
                    if diff_y > 20: is_toolbar_area = False
                    elif cy > h * 0.12: is_toolbar_area = False
                if "menu" in tag or "menu" in role: is_toolbar_area = False
                if lower_name in ("apply", "change store", "search"): is_toolbar_area = False

                if is_toolbar_area:
                    if tag in ("push-button", "entry", "combo-box", "menu-item", "toggle-button"):
                        if not dry_run:
                            if tag == "entry": n["tag"] = "browser-entry"
                            elif tag == "combo-box": n["tag"] = "browser-combo"
                            else: n["tag"] = "browser-button"
                        regions["BROWSER_UI"].append(n)
                    continue

            # Priority 3: Browser Tabs
            tab_cy_ok = TABSTRIP_Y_MIN <= cy <= TABSTRIP_Y_MAX
            not_in_win_controls = x < win_controls_min_x

            if lower_name in BROWSER_TAB_ANCHORS and tab_cy_ok and not_in_win_controls:
                if not dry_run:
                    if "tab" in role or tag == "page tab": n["tag"] = "browser-tab"
                    else: n["tag"] = "browser-tab-button"
                regions["BROWSER_TABS"].append(n)
                continue

            # Priority 4: APP_LAUNCHER
            if x <= LAUNCHER_X_MAX and bbox["w"] <= ICON_W_MAX and bbox["h"] >= 40:
                if tag in ("push-button", "toggle-button"):
                    if not dry_run: n["tag"] = "launcher-app"
                    regions["APP_LAUNCHER"].append(n)
                    continue 

            # Priority 5: Content
            if not label: continue
            if len(label) == 1 and not label.isalnum(): continue
            if label in ("ADVERTISEMENT",): continue
            
            if tag == "static" and role == "heading":
                if not dry_run: n["tag"] = "heading"
            
            if tag == "list-item" and "result" in label.lower():
                if not dry_run: n["tag"] = "static"

            regions["CONTENT"].append(n)

        if regions["CONTENT"]:
            regions["CONTENT"] = self._dedup_overlapping_content(regions["CONTENT"])

        return regions

    def get_meta_header(self, regions: Dict[str, List[Node]]) -> List[str]:
        raw_url = ""
        for n in regions.get("BROWSER_UI", []):
            if "address" in (n.get("name") or "").lower() and n.get("tag") in ("entry", "browser-entry"):
                raw_url = n.get("text") or ""
                break
        if not raw_url: return []
        return [f"URL: {self._format_url(raw_url)}"]

    def _format_url(self, raw_url):
        tmp = raw_url.strip()
        if "://" not in tmp: tmp = "https://" + tmp
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
        filtered_nodes = [n for n in nodes if not self._should_skip_for_content(n)]
        tuples = self._nodes_to_tuples(filtered_nodes)
        tuples.sort()        
        y_tol = int(screen_h * 0.03)
        x_tol = int(screen_w * 0.15)
        if self.enable_static_line_merge:
            tuples = merge_fragmented_static_lines(tuples, y_tol, x_tol)
        return build_hierarchical_content_lines(tuples, big_gap_px=None, heading_section_gap_px=None)