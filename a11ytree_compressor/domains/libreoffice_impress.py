import re
from typing import List, Dict, Tuple, Set, Optional
from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    build_hierarchical_content_lines, dedup_horizontal_menu_nodes
)
from ..a11y_instruction_utils import summarize_calc_instruction


class LibreOfficeImpressCompressor(BaseA11yCompressor):
    domain_name = "libreoffice_impress"
    
    # 背景削除は誤判定のリスクがあるためFalse、ステータスバーは情報源として使う
    enable_background_filtering = False
    use_statusbar = True 

    MENU_KEYWORDS: Set[str] = {
        "file", "edit", "view", "insert", "format",
        "slide", "slide show",
        "tools", "window", "help"
    }

    MODAL_KEYWORDS: Set[str] = {
        "document in use",
        "save", "open", "print", "properties"
    }

    def split_static_ui(self, nodes: List[Node], w: int, h: int) -> Tuple[List[Node], List[Node]]:
        regions = self.get_semantic_regions(nodes, w, h, dry_run=True)
        
        static_nodes = []
        dynamic_nodes = [] 
        
        # 動かないUI要素のグループ
        static_groups = [
            "MENUBAR", "APP_LAUNCHER", "TOOLBAR",
            "STATUSBAR", "SLIDE_LIST", "PROPERTIES"
        ]

        static_ids = set()
        for group in static_groups:
            for n in regions.get(group, []):
                static_ids.add(id(n))
        
        for n in nodes:
            if id(n) in static_ids:
                static_nodes.append(n)
            else:
                dynamic_nodes.append(n)

        return dynamic_nodes, static_nodes

    def get_semantic_regions(
        self, nodes: List[Node], w: int, h: int, dry_run: bool = False
    ) -> Dict[str, List[Node]]:
        regions = {
            "MENUBAR": [],
            "APP_LAUNCHER": [],
            "TOOLBAR": [],
            "SLIDE_LIST": [],
            "CONTENT": [],
            "PROPERTIES": [],
            "STATUSBAR": [],
            "MODAL": [],
        }

        # しきい値設定
        LAUNCHER_X_LIMIT = w * 0.05

        # 上部バー領域（メニュー＋ツールバー）
        TOP_BAR_MAX_Y = h * 0.20  # 上から 20% くらいまでを「バー領域」とみなす

        # メインエリア（スライドや左右パネル）
        MAIN_BOTTOM = h * 0.93

        # 左右の境界
        SLIDE_LIST_RIGHT = w * 0.20   # 左サイドバーの幅
        PROPERTIES_LEFT  = w * 0.80   # 右サイドバーの開始位置
        MENUBAR_MAX_Y    = h * 0.10
        TOOLBAR_Y_TOP    = h * 0.08
        TOOLBAR_Y_BOTTOM = h * 0.18
        MAIN_TOP         = h * 0.15
        MAIN_BOTTOM      = h * 0.95


        for n in nodes:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

            tag = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            name_lower = name.lower()

            # 1. APP_LAUNCHER (左端のランチャーアイコン)
            if x < LAUNCHER_X_LIMIT and bw < w * 0.06 and bh > 30:
                if tag in ("push-button", "toggle-button", "launcher-app"):
                    regions["APP_LAUNCHER"].append(n)
                    continue

            # 2. MODAL (キーワード判定は先に抜いておく)
            if any(key in name_lower for key in self.MODAL_KEYWORDS):
                regions["MODAL"].append(n)
                continue

            # 3. STATUSBAR (最下部)
            if cy > h * 0.96:
                regions["STATUSBAR"].append(n)
                continue

            # 4. 上部バー領域 (MENUBAR / TOOLBAR)
            if cy < TOP_BAR_MAX_Y:
                # 4-1) メニューバー (menuタグ＋キーワード一致)
                if cy < MENUBAR_MAX_Y and tag == "menu" and name_lower in self.MENU_KEYWORDS:
                    regions["MENUBAR"].append(n)
                    continue

                # 4-2) ツールバー (ボタン類)
                if TOOLBAR_Y_TOP < cy < TOOLBAR_Y_BOTTOM:
                    if tag in ("push-button", "toggle-button", "combo-box", "entry", "textbox", "tool-bar"):
                        regions["TOOLBAR"].append(n)
                        continue

            # 5. メインエリア → SLIDE_LIST / CONTENT / PROPERTIES
                if MAIN_TOP <= cy <= MAIN_BOTTOM:
                    if cx < SLIDE_LIST_RIGHT:
                        regions["SLIDE_LIST"].append(n)
                        continue

                if cx > PROPERTIES_LEFT:
                    regions["PROPERTIES"].append(n)
                    continue

                # 中央（スライドキャンバス）
                regions["CONTENT"].append(n)
                continue

            # 6. フォールバック（どうしても分類できないものはCONTENTへ）
            regions["CONTENT"].append(n)

        return regions

    # -------------------------------------------------------------------------
    # 中心座標
    # -------------------------------------------------------------------------
    def _format_center(self, node: Node) -> str:
        """ノードの中心座標を '@ (cx, cy)' 形式の文字列で返す"""
        bbox = node_bbox_from_raw(node)
        cx, cy = bbox_to_center_tuple(bbox)
        return f"@ ({cx}, {cy})"

    # -------------------------------------------------------------------------
    # 各セクションごとの圧縮ロジック
    # -------------------------------------------------------------------------


    def _compress_menubar(self, nodes: List[Node]) -> List[str]:
        """メニューバーの圧縮。位置ベースのdedup + ラベル重複の除去"""
        if not nodes:
            return []

        # x座標でソートして左→右の順にそろえる
        nodes = sorted(nodes, key=lambda n: node_bbox_from_raw(n)["x"])

        # 位置ベースの重複除去（同じ列に重なっているものをまとめる想定）
        deduped_nodes = dedup_horizontal_menu_nodes(nodes, 0, 0)

        lines: List[str] = []
        seen_names: Set[str] = set()

        for n in deduped_nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            if name in seen_names:
                continue
            seen_names.add(name)
            center_str = self._format_center(n)
            lines.append(f"[menu] {name} {center_str}")
        return lines


    def _compress_toolbar(self, nodes: List[Node]) -> List[str]:
        lines = []
        for n in nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            center_str = self._format_center(n)
            lines.append(f"[toolbar] {name} {center_str}")
        return lines

    def _compress_statusbar(self, nodes: List[Node]) -> List[str]:
        lines = []
        seen = set()
        for n in nodes:
            text = (n.get("name") or n.get("text") or "").strip()
            if text and text not in seen:
                seen.add(text)
                center_str = self._format_center(n)
                lines.append(f"[status] {text} {center_str}")
        return lines


    def _compress_slide_list(self, nodes: List[Node]) -> List[str]:
        lines: List[str] = []
        for n in nodes:
            label = (n.get("name") or n.get("text") or "").strip()
            if not label:
                continue
            center_str = self._format_center(n)
            lines.append(f"[slide-list] {label} {center_str}")
        return lines



    def _compress_properties(self, nodes: List[Node]) -> List[str]:
        """右サイドバー（プロパティ）の圧縮"""
        lines = []
        for n in nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            tag = (n.get("tag") or "").lower()

            # 入力可能な要素やラベルを優先
            if tag in ("push-button", "combo-box", "check-box", "label") and name:
                center_str = self._format_center(n)
                lines.append(f"[property] {name} {center_str}")
        return lines

    def _compress_content(self, nodes: List[Node]) -> List[str]:
        """中央のスライド編集エリアの圧縮"""
        lines = []
        for n in nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            role = (n.get("role") or "").lower()
            tag = (n.get("tag") or "").lower()
            
            if not name:
                continue
            
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            
            prefix = "content"
            if "title" in role or "heading" in role:
                prefix = "slide-title"
            elif role == "presentation_shape" or tag == "shape":
                prefix = "shape"
            elif tag == "image":
                prefix = "image"
            
            lines.append(f"[{prefix}] \"{name}\" @ ({cx}, {cy})")
            
        return lines

    def _compress_modal(self, nodes: List[Node], w: int, h: int) -> List[str]:
        return self.process_region_lines(nodes, w, h)

    def _build_output(
        self,
        regions: Dict[str, List[Node]],
        modal_nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> List[str]:
        lines: List[str] = []

        # 1. APP_LAUNCHER
        if regions.get("APP_LAUNCHER"):
            lines.append("APP_LAUNCHER:")
            # process_region_lines は BaseA11yCompressor のメソッドで、List[str] を返す想定
            lines.extend(self.process_region_lines(
                regions["APP_LAUNCHER"], screen_w, screen_h
            ))

        # 2. STATUSBAR
        status_lines = self._compress_statusbar(regions.get("STATUSBAR", []))
        if status_lines:
            lines.append("STATUSBAR:")
            lines.extend(status_lines)

        # 3. MENUBAR (ここが修正箇所: Nodeリストではなく文字列リストを受け取る)
        menubar_lines = self._compress_menubar(regions.get("MENUBAR", []))
        if menubar_lines:
            lines.append("MENUBAR:")
            lines.extend(menubar_lines)

        # 4. TOOLBAR
        toolbar_lines = self._compress_toolbar(regions.get("TOOLBAR", []))
        if toolbar_lines:
            lines.append("TOOLBAR:")
            lines.extend(toolbar_lines)

        # 5. SLIDE_LIST (左)
        slide_lines = self._compress_slide_list(regions.get("SLIDE_LIST", []))
        if slide_lines:
            lines.append("SLIDE_LIST:")
            lines.extend(slide_lines)

        # 6. PROPERTIES (右)
        prop_lines = self._compress_properties(regions.get("PROPERTIES", []))
        if prop_lines:
            lines.append("PROPERTIES:")
            lines.extend(prop_lines)

        # 7. CONTENT (中央)
        content_lines = self._compress_content(regions.get("CONTENT", []))
        if content_lines:
            lines.append("CONTENT:")
            lines.extend(content_lines)

        # 8. MODAL
        if modal_nodes:
            lines.append("MODAL:")
            lines.extend(self._compress_modal(modal_nodes, screen_w, screen_h))

        return lines