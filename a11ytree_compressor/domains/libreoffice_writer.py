import re
from typing import List, Dict, Tuple, Set, Optional, Any
from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    build_hierarchical_content_lines, dedup_horizontal_menu_nodes
)
from ..a11y_instruction_utils import summarize_calc_instruction


class LibreOfficeWriterCompressor(BaseA11yCompressor):
    domain_name = "libreoffice_writer"
    
    # 背景削除は誤判定のリスクがあるためFalse、ステータスバーは情報源として使う
    enable_background_filtering = False
    use_statusbar = True 

    MENU_KEYWORDS: Set[str] = {
        # LibreOffice Writer のメニューバー
        "file", "edit", "view", "insert", "format",
        "styles", "table", "form",
        "tools", "window", "help"
    }

    MODAL_KEYWORDS: Set[str] = {
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

        LAUNCHER_X_LIMIT = w * 0.05
        TOP_BAR_MAX_Y   = h * 0.20  # 上 20% は「バー領域」

        SLIDE_LIST_RIGHT = w * 0.20
        PROPERTIES_LEFT  = w * 0.80
        MENUBAR_MAX_Y    = h * 0.10
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

            # 1. APP_LAUNCHER
            if x < LAUNCHER_X_LIMIT and bw < w * 0.06 and bh > 30:
                if tag in ("push-button", "toggle-button", "launcher-app"):
                    regions["APP_LAUNCHER"].append(n)
                    continue

            # 2. MODAL
            if any(kw in name_lower for kw in self.MODAL_KEYWORDS):
                regions["MODAL"].append(n)
                continue

            # 3. STATUSBAR（最下部）
            if cy > h * 0.92:
                if tag in ("statusbar", "status", "label"):
                    regions["STATUSBAR"].append(n)
                    continue

            # 4. 上部バー領域 (MENUBAR / TOOLBAR)
            if cy < TOP_BAR_MAX_Y:
                # 4-1) MENUBAR
                if cy < MENUBAR_MAX_Y and tag == "menu" and name_lower in self.MENU_KEYWORDS:
                    regions["MENUBAR"].append(n)
                    continue

                # 4-2) TOOLBAR: 上部にあるボタン・テキスト類はすべてツールバー扱い
                if tag in (
                    "push-button", "toggle-button", "combo-box",
                    "entry", "textbox", "tool-bar", "text"
                ):
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

                regions["CONTENT"].append(n)
                continue

            # 6. フォールバック
            regions["CONTENT"].append(n)

        return regions


    # === 圧縮系ユーティリティ ===

    def _format_center(self, n: Node) -> str:
        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        return f"@ ({cx}, {cy})"

    # === 各領域の圧縮ロジック ===

    def _compress_menubar(self, nodes: List[Node]) -> List[str]:
        """メニューバー (File / Edit / View / ...) の圧縮"""
        lines: List[str] = []

        # 横並びのメニューをdedup
        deduped = dedup_horizontal_menu_nodes(nodes)

        seen_names = set()
        for n in deduped:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            lower = name.lower()
            if lower not in self.MENU_KEYWORDS:
                continue
            if lower in seen_names:
                continue
            seen_names.add(lower)
            center_str = self._format_center(n)
            lines.append(f"[menu] \"{name}\" {center_str}")
        return lines

    def _compress_toolbar(self, nodes: List[Node]) -> List[str]:
        lines = []
        for n in nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            tag = (n.get("tag") or "").lower()
            name_lower = name.lower()
            center_str = self._format_center(n)

            # 特別扱い: スタイル / フォント / フォントサイズ
            if tag in ("text", "entry", "combo-box"):
                if "normal" in name_lower:
                    prefix = "[style-preset]"
                elif " pt" in name_lower or name_lower.endswith("pt"):
                    prefix = "[font-size]"
                else:
                    prefix = "[font]"
            else:
                # それ以外のボタン・トグル
                if tag == "toggle-button":
                    prefix = "[toolbar-toggle]"
                else:
                    prefix = "[toolbar-btn]"

            lines.append(f"{prefix} \"{name}\" {center_str}")
        return lines


    def _compress_slide_list(self, nodes: List[Node]) -> List[str]:
        """左サイドバー用（Writer ではナビゲーター等が入る想定, 現状ほぼ空）"""
        lines: List[str] = []
        for n in nodes:
            label = (n.get("name") or n.get("text") or "").strip()
            if not label:
                continue
            center_str = self._format_center(n)
            lines.append(f"[sidebar-left] {label} {center_str}")
        return lines

    def _compress_properties(self, nodes: List[Node]) -> List[str]:
        """右サイドバー（プロパティ）の圧縮（暫定：ファイル名等は除外）"""
        lines = []
        for n in nodes:
            name = (n.get("name") or (n.get("text") or "")).strip()
            if not name:
                continue

            tag = (n.get("tag") or "").lower()
            name_lower = name.lower()

            # スクロールバーはスキップ
            if tag == "scroll-bar" or "scroll bar" in name_lower:
                continue

            # ファイル名・ロックファイルっぽいものは拡張子ベースで除外
            if "docx" in name_lower or "odt" in name_lower or "doc#" in name_lower:
                continue
            if "lock" in name_lower:
                continue

            center_str = self._format_center(n)
            lines.append(f"[prop] \"{name}\" {center_str}")
        return lines



    def _compress_statusbar(self, nodes: List[Node]) -> List[str]:
        """ステータスバー (ページ番号 / 単語数 / 言語 / ズーム率など)"""
        lines = []
        for n in nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            name_lower = name.lower()
            # Unity の「Home」ラベルなどは除外
            if name_lower == "home":
                continue

            center_str = self._format_center(n)
            lines.append(f"[status] \"{name}\" {center_str}")
        return lines


    def _compress_content(self, nodes: List[Node]) -> List[str]:
        """
        本文エリア（中央）の簡易圧縮（Writer初期版）
        - paragraph / heading / image / document-text を対象にする
        - 各タイプごとに番号を振る
        """
        lines: List[str] = []

        para_idx = 1
        heading_idx = 1
        image_idx = 1
        doc_idx = 1  # 複数ページがあれば将来使えるように一応用意

        for n in nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            tag = (n.get("tag") or "").lower()

            # document-text はページ全体のコンテナなので、名前がなくても許す
            if tag != "document-text" and not name:
                continue

            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)

            if tag == "paragraph":
                prefix = f"paragraph-{para_idx}"
                para_idx += 1

            elif tag == "heading":
                prefix = f"heading-{heading_idx}"
                heading_idx += 1

            elif tag == "image":
                prefix = f"image-{image_idx}"
                image_idx += 1
                # 画像はファイル名だけだと分かりづらいので、将来 alt-text などがあればここで使う想定

            elif tag == "document-text":
                # ページ全体コンテナ。頻繁に出すとうるさいので 1 ページ 1 行程度。
                prefix = f"document-{doc_idx}"
                doc_idx += 1

            else:
                # それ以外は今は無視（必要になったらここに追加）
                continue

            # document-text は name が空のこともあるので fallback
            if not name and tag == "document-text":
                name = "LibreOffice Document"

            lines.append(f"[{prefix}] \"{name}\" @ ({cx}, {cy})")

        return lines

    def _compress_modal(self, nodes: List[Node], w: int, h: int) -> List[str]:
        return self.process_region_lines(nodes, w, h)

    def _filter_modal_nodes(self, modal_nodes: List[Node], w: int, h: int) -> List[Node]:
        """
        Writer 用（暫定）:
        ダイアログ内に複製されているメニューバー (File/Edit/...) を
        MODAL から除外する。
        """
        filtered: List[Node] = []
        TOP_LIMIT = h * 0.20  # 画面上部 20% くらいまで

        for n in modal_nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            name_lower = name.lower()
            bbox = node_bbox_from_raw(n)
            _, cy = bbox_to_center_tuple(bbox)

            # 画面上部にある menu タグで、メニューキーワードに一致するものは除外
            if cy < TOP_LIMIT and tag == "menu" and name_lower in self.MENU_KEYWORDS:
                continue

            filtered.append(n)

        return filtered

    # === メイン圧縮関数 ===
    def _build_output(
        self,
        regions: Dict[str, List[Node]],
        modal_nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> List[str]:
        lines: List[str] = []
        """
        LibreOffice Writer 向けの初期版コンプレッサ。
        ・UIを MENUBAR / TOOLBAR / CONTENT / PROPERTIES / STATUSBAR / MODAL 等に分割
        ・各領域をそれぞれ簡易なテキストに変換
        instruction_keywords / use_instruction は現状未使用だが、
        API 互換のために受け取っておく。
        """
        # APP_LAUNCHER
        if regions.get("APP_LAUNCHER"):
            lines.append("APP_LAUNCHER:")
            # process_region_lines は BaseA11yCompressor のメソッドで、List[str] を返す想定
            lines.extend(self.process_region_lines(
                regions["APP_LAUNCHER"], screen_w, screen_h
            ))

        # MENUBAR
        menubar_lines = self._compress_menubar(regions.get("MENUBAR", []))
        if menubar_lines:
            lines.append("MENUBAR:")
            lines.extend(menubar_lines)

        # TOOLBAR
        toolbar_lines = self._compress_toolbar(regions.get("TOOLBAR", []))
        if toolbar_lines:
            lines.append("TOOLBAR:")
            lines.extend(toolbar_lines)

        # SLIDE_LIST（Writerではほぼ空だが一応）
        slide_list_lines = self._compress_slide_list(regions.get("SLIDE_LIST", []))
        if slide_list_lines:
            lines.append("SLIDE_LIST:")
            lines.extend(slide_list_lines)

        # STATUSBAR
        status_lines = self._compress_statusbar(regions.get("STATUSBAR", []))
        if status_lines:
            lines.append("STATUSBAR:")
            lines.extend(status_lines)

        # PROPERTIES（右サイドバー）
        prop_lines = self._compress_properties(regions.get("PROPERTIES", []))
        if prop_lines:
            lines.append("PROPERTIES:")
            lines.extend(prop_lines)

        # CONTENT（中央本文）
        content_lines = self._compress_content(regions.get("CONTENT", []))
        if content_lines:
            lines.append("CONTENT:")
            lines.extend(content_lines)

        # MODAL
        if modal_nodes:
            modal_nodes = self._filter_modal_nodes(modal_nodes, screen_w, screen_h)
            if modal_nodes:
                lines.append("MODAL:")
                lines.extend(self._compress_modal(modal_nodes, screen_w, screen_h))

        # 他ドメインと同じように dict で返す
        return lines