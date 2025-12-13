import re
import sys
from typing import List, Dict, Tuple, Set, Optional, Any
from collections import defaultdict
from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    build_hierarchical_content_lines
)

class ThunderbirdCompressor(BaseA11yCompressor):
    domain_name = "thunderbird"
    
    enable_background_filtering = False
    use_statusbar = True 

    # モーダル判定用キーワード
    MODAL_KEYWORDS: Set[str] = {
        "save as", "print", "password", 
        "alert", "confirm"
    }
    
    # Home画面(Dashboard)特有のキーワード
    DASHBOARD_KEYWORDS: Set[str] = {
    "read messages", "write a new message", "search messages",
    "manage message filters",
    "set up another account", "import from another program",
    "create a new calendar", "create a new address book",
    "connect to your existing email account",
    "connect to your chat account",
    "set up filelink",
    "connect to feeds",
    "connect to a newsgroup",
    "import data from other programs",
    "end-to-end encryption",
    "explore features", "make a donation",
    "support", "get involved", "developer documentation",
    }

    ACCOUNT_SETUP_BUTTON_SHORT: Dict[str, str] = {
        "Connect to your existing email account": "Email",
        "Create a new address book": "Address Book",
        "Create a new calendar": "Calendar",
        "Connect to your chat account": "Chat",
        "Set up Filelink": "Filelink",
        "Connect to feeds": "Feeds",
        "Connect to a newsgroup": "Newsgroups",
        "Import data from other programs": "Import",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._prev_view_type = None
        self._view_change_cooldown = 0  # view切替直後のフレーム数（1 or 2 推奨）



    def get_semantic_regions(
        self, nodes: List[Node], w: int, h: int, dry_run: bool = False
    ) -> Dict[str, List[Node]]:
        regions: Dict[str, List[Node]] = {
            "APP_LAUNCHER": [],
            "TOP_BAR": [],
            "SPACES_BAR": [],   # 左端のアイコンバー
            "TOOLBAR": [],      # 最上部の検索バーなど
            "SIDEBAR_HEADER": [], # フォルダツリー上のボタン(New Message等)
            "SIDEBAR": [],      # フォルダツリー本体
            "MESSAGE_LIST": [], # メール一覧
            "PREVIEW": [],      # メール本文
            "DASHBOARD": [],    # Home画面
            "MODAL": [],
            "STATUSBAR": [],
            "CONTENT": [],
            "FOLDER_TREE": [], 
            "HOME_DASHBOARD": [], 
            "MAIL_TOOLBAR": [], 
        }

        # --- 座標定数 (1920x1080想定で調整) ---
        LAUNCHER_X_LIMIT = w * 0.05
        TOP_BAR_MAX_Y    = 50
        SPACES_BAR_MAX_X = 115 
        SPLIT_SIDEBAR_X = 400 
        SPLIT_LIST_X    = w * 0.55
        TB_TOOLBAR_BOTTOM_Y = 100 
        SIDEBAR_HEADER_BOTTOM_Y = 150
        BOTTOM_AREA_Y = 1060 

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            cx, cy = bbox_to_center_tuple(bbox)

            tag  = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            name_lower = name.lower()
            
            # --- 1. MODAL ---
            is_control = tag in {"push-button", "toggle-button", "link", "menu-item", "menu"}
            if role in {"dialog", "alert"} or (
                not is_control and any(k in name_lower for k in self.MODAL_KEYWORDS)
            ):
                regions["MODAL"].append(n)
                continue

            # --- 2. OS / System UI ---
            if x < LAUNCHER_X_LIMIT and bh > 32 and bw < w * 0.12 and tag in {"push-button", "toggle-button", "launcher-app"}:
                regions["APP_LAUNCHER"].append(n)
                continue

            if cy < TOP_BAR_MAX_Y:
                regions["TOP_BAR"].append(n)
                continue
            
            # --- 3. Status Bar (最優先判定) ---
            # ★修正: 名前完全一致なら座標無視でステータスバーへ
            if name in {"You are currently online.", "Done", "Unread:", "Total:"}:
                regions["STATUSBAR"].append(n)
                continue

            # 座標判定: 画面最下部
            if cy > BOTTOM_AREA_Y and cy < 1080:
                regions["STATUSBAR"].append(n)
                continue

            # --- 4. Thunderbird Left Columns ---
            if cx < SPACES_BAR_MAX_X and bw < 60:
                regions["SPACES_BAR"].append(n)
                continue

            if SPACES_BAR_MAX_X <= cx < SPLIT_SIDEBAR_X:
                if cy < SIDEBAR_HEADER_BOTTOM_Y:
                    regions["SIDEBAR_HEADER"].append(n)
                else:
                    regions["FOLDER_TREE"].append(n)
                continue

            # --- 5. Main Content Area ---
            if cy < TB_TOOLBAR_BOTTOM_Y:
                regions["TOOLBAR"].append(n)
                continue

            if any(k in name_lower for k in self.DASHBOARD_KEYWORDS) or \
               (name_lower in {"address book", "account settings", "settings"}):
                regions["HOME_DASHBOARD"].append(n)
                continue
            
            if cx < SPLIT_LIST_X:
                if regions["HOME_DASHBOARD"] and tag in {"heading", "paragraph", "label"} and bh > 20:
                     regions["HOME_DASHBOARD"].append(n)
                else:
                     regions["MESSAGE_LIST"].append(n)
            else:
                if regions["HOME_DASHBOARD"] and tag in {"heading", "paragraph", "label", "link"}:
                     regions["HOME_DASHBOARD"].append(n)
                else:
                     regions["PREVIEW"].append(n)

        return regions

    # === フォーマット用ヘルパー ===
    def _format_node(self, n: Node) -> str:
        """標準的な [tag] "name" @ (cx, cy) 形式で出力"""
        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or n.get("text") or "").strip()
        
        if not name:
            return ""
            
        return f"[{tag}] \"{name}\" @ ({cx}, {cy})"

    # === 圧縮関数群 ===

    def _compress_app_launcher(self, nodes: List[Node]) -> List[str]:
        lines = []
        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[1]
        )
        seen = set()
        for n in sorted_nodes:
            line = self._format_node(n)
            if not line or line in seen: continue
            seen.add(line)
            lines.append(line)
        return lines

    def _compress_top_bar(self, nodes: List[Node]) -> List[str]:
        lines = []
        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[0]
        )
        seen = set()
        for n in sorted_nodes:
            line = self._format_node(n)
            if not line or line in seen: continue
            seen.add(line)
            lines.append(line)
        return lines

    def _compress_spaces_bar(self, nodes: List[Node]) -> List[str]:
        lines = []
        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[1]
        )
        seen = set()
        for n in sorted_nodes:
            line = self._format_node(n)
            if not line or line in seen: continue
            seen.add(line)
            lines.append(line)
        return lines

    def _compress_toolbar(self, nodes: List[Node]) -> List[str]:
        lines = []
        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[0]
        )
        seen = set()
        for n in sorted_nodes:
            name = (n.get("name") or "").strip()
            if name in {"Minimize", "Restore Down", "Close", "AppMenu"}:
                continue
            
            line = self._format_node(n)
            if not line or line in seen: continue
            seen.add(line)
            lines.append(line)
        return lines
        

    def _compress_folder_tree(self, nodes: List[Node]) -> List[str]:
        """フォルダツリー: ルート名(@, Local Folders)ベースで階層表現 + 座標"""
        if not nodes:
            return []

        items: List[Node] = [
            n for n in nodes
            if (n.get("tag") or "").lower() == "tree-item"
        ]
        if not items:
            return []

        items.sort(
            key=lambda n: (
                node_bbox_from_raw(n)["y"],
                node_bbox_from_raw(n)["x"],
            )
        )

        def is_root_name(name: str) -> bool:
            if not name:
                return False
            lower = name.lower()
            if "@" in name:
                return True
            if lower == "local folders":
                return True
            return False

        groups: List[Tuple[Optional[Node], List[Node]]] = []
        current_root: Optional[Node] = None

        for n in items:
            name = (n.get("name") or "").strip()
            if not name:
                continue

            if is_root_name(name):
                current_root = n
                groups.append((n, []))
            else:
                if current_root is None:
                    groups.append((None, [n]))
                else:
                    groups[-1][1].append(n)

        lines: List[str] = []
        seen_keys: Set[str] = set()

        for root, children in groups:
            if root is not None:
                root_line = self._format_node(root)
                if root_line and root_line not in seen_keys:
                    seen_keys.add(root_line)
                    lines.append(root_line)

                for c in children:
                    child_line = self._format_node(c)
                    if not child_line or child_line in seen_keys:
                        continue
                    seen_keys.add(child_line)
                    lines.append("  " + child_line)
            else:
                for c in children:
                    child_line = self._format_node(c)
                    if not child_line or child_line in seen_keys:
                        continue
                    seen_keys.add(child_line)
                    lines.append(child_line)

        return lines


    # === Home Dashboard のセクション分割ロジック ===
    def _split_home_sections(self, nodes: List[Node]) -> Dict[str, List[Node]]:
        sections: Dict[str, List[Node]] = {}
        nodes = sorted(nodes, key=lambda n: node_bbox_from_raw(n)["y"])
        current_section = "Unknown"
        sections[current_section] = []
        
        section_headers = {
            "Set Up Another Account",
            "Import from Another Program",
            "About Mozilla Thunderbird",
            "Resources"
        }

        for n in nodes:
            name = (n.get("name") or "").strip()
            if name in section_headers:
                current_section = name
                if current_section not in sections:
                    sections[current_section] = []
                sections[current_section].append(n)
            else:
                sections[current_section].append(n)
                
        return sections

    def _compress_home_dashboard(self, nodes: List[Node]) -> List[str]:
        if not nodes: return []

        sections = self._split_home_sections(nodes)
        
        sorted_sections = []
        for title, section_nodes in sections.items():
            if section_nodes:
                min_y = min(node_bbox_from_raw(n)["y"] for n in section_nodes)
                sorted_sections.append((min_y, title, section_nodes))
        sorted_sections.sort(key=lambda x: x[0])

        lines: List[str] = []
        seen_keys = set()

        all_section_node_ids = {id(n) for _, _, sn in sorted_sections for n in sn}
        orphans = [n for n in nodes if id(n) not in all_section_node_ids]
        
        if orphans:
            orphans.sort(key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
            for n in orphans:
                l = self._format_node(n)
                if l and l not in seen_keys:
                    seen_keys.add(l)
                    lines.append(l)
            if lines: lines.append("")

        for _, title, section_nodes in sorted_sections:
            section_nodes.sort(key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
            
            for n in section_nodes:
                node_for_print = n
                tag = (n.get("tag") or "").lower()
                name = (n.get("name") or "").strip()

                if title == "Set Up Another Account" and tag == "push-button":
                    short = self.ACCOUNT_SETUP_BUTTON_SHORT.get(name)
                    if short:
                        node_copy = dict(n)
                        node_copy["name"] = short
                        node_for_print = node_copy

                l = self._format_node(node_for_print)
                if not l or l in seen_keys:
                    continue
                seen_keys.add(l)
                lines.append(l)

            lines.append("")

        return lines

    def _compress_message_list(self, nodes: List[Node]) -> List[str]:
        lines = []
        nodes.sort(key=lambda n: node_bbox_from_raw(n)["y"])
        seen = set()
        for n in nodes:
            line = self._format_node(n)
            if not line or line in seen: continue
            seen.add(line)
            lines.append(line)
        return lines

    def _compress_preview(self, nodes: List[Node]) -> List[str]:
        lines = []
        nodes.sort(key=lambda n: node_bbox_from_raw(n)["y"])
        for n in nodes:
            line = self._format_node(n)
            if line: lines.append(line)
        return lines

    def _compress_statusbar(self, nodes: List[Node]) -> List[str]:
        lines = []
        nodes.sort(key=lambda n: node_bbox_from_raw(n)["x"])
        for n in nodes:
            bbox = node_bbox_from_raw(n)
            if bbox["y"] > 1080: 
                continue
            line = self._format_node(n)
            if line: lines.append(line)
        return lines

    def _compress_modal(self, nodes: List[Node]) -> List[str]:
        lines = []
        nodes.sort(key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        for n in nodes:
            line = self._format_node(n)
            if line: lines.append(line)
        return lines
    
    # ==== Settings helpers ====

    def _split_by_vertical_position(
        self,
        nodes: List[Node],
        screen_h: int,
        visible_ratio: float = 1.1,
        scroll_ratio: float = 2.5,
    ) -> Tuple[List[Node], List[Node], List[Node]]:
        """
        Split nodes into:
        - visible       : roughly within current viewport
        - below_fold    : 1〜2画面分くらいスクロールしたあたり
        - deep          : それよりさらに下（通常は捨てる）
        """
        if not nodes:
            return [], [], []

        visible_limit = int(screen_h * visible_ratio)
        scroll_limit = int(screen_h * scroll_ratio)

        visible, below_fold, deep = [], [], []
        for n in nodes:
            cy = bbox_to_center_tuple(node_bbox_from_raw(n))[1]
            if cy <= visible_limit:
                visible.append(n)
            elif cy <= scroll_limit:
                below_fold.append(n)
            else:
                deep.append(n)
        return visible, below_fold, deep

    def _compress_settings_sidebar(self, nodes: List[Node]) -> List[str]:
        """
        Settings 左サイドバー: 
        ナビゲーションに不要なタグ（menu-item, push-button等）を除外し、
        純粋なカテゴリ（list-item, link）のみを残す。
        """
        if not nodes:
            return []

        # 重複排除用の優先順位定義
        TAG_PRIORITY = {
            "link": 3,
            "list-item": 2,
            "tree-item": 2,
            # label, push-button, menu-item はサイドバーナビゲーションではないので除外
        }

        # 名前ごとにノードをグルーピング
        grouped: Dict[str, List[Node]] = {}
        for n in nodes:
            name = (n.get("name") or "").strip()
            if not name: 
                continue
            
            # ★追加フィルタ: サイドバーとして不適切なタグを除外
            tag = (n.get("tag") or "").lower()
            if tag not in TAG_PRIORITY:
                continue

            # ★追加フィルタ: x座標が明らかに右側にあるもの(Doneなど)が混ざらないようガード
            bbox = node_bbox_from_raw(n)
            if bbox["x"] > 350:
                continue

            if name not in grouped:
                grouped[name] = []
            grouped[name].append(n)

        unique_nodes = []
        for name, group in grouped.items():
            best_node = sorted(
                group, 
                key=lambda n: (
                    -TAG_PRIORITY.get((n.get("tag") or "").lower(), 0), 
                    node_bbox_from_raw(n)["y"] 
                )
            )[0]
            unique_nodes.append(best_node)

        unique_nodes.sort(key=lambda n: node_bbox_from_raw(n)["y"])

        lines: List[str] = []
        seen = set()
        for n in unique_nodes:
            line = self._format_node(n)
            if line and line not in seen:
                seen.add(line)
                lines.append(line)

        return lines

    def _compress_settings_main(self, nodes: List[Node], fold_y: int) -> List[str]:
        """
        Settings 本体の「画面内」に見えている部分。
        fold_y (折り返し地点) より下にあるものは除外する。
        """
        if not nodes:
            return []

        nodes = sorted(nodes, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        lines: List[str] = []
        seen = set()

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").strip()

            if tag in {"document-web"}:
                continue

            # ★追加: Status Barに分類されるべきものが紛れ込んでいたら除外
            # (get_semantic_regionsで分類しきれなかった場合の安全策)
            if name in {"Home", "Done", "You are currently online."} and node_bbox_from_raw(n)["y"] > 1000:
                continue

            # 渡された fold_y (1080など) を基準に判定
            bbox = node_bbox_from_raw(n)
            if bbox["y"] > fold_y:
                continue

            line = self._format_node(n)
            if not line or line in seen:
                continue
            seen.add(line)
            lines.append(line)

        return lines

    def _compress_settings_below_fold(self, nodes: List[Node]) -> List[str]:
        """
        現在の viewport から下にスクロールしたときに現れる設定項目。
        情報量を減らすため、主に heading / label / list-item だけを出す。
        """
        if not nodes:
            return []

        nodes = sorted(nodes, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        lines: List[str] = []
        seen = set()

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag not in {"heading", "label", "list-item"}:
                continue

            line = self._format_node(n)
            if not line or line in seen:
                continue
            seen.add(line)
            lines.append(line)

        return lines

    def _compress_settings_view(
        self,
        regions: Dict[str, List[Node]],
        screen_w: int,
        screen_h: int,
    ) -> List[str]:
        lines: List[str] = []
        
        # 強制的に 1080px (FHD相当) を境界線として設定
        fold_y = 1080

        # 必要な領域を統合 (MESSAGE_LISTなども含めて全量をチェック)
        all_settings_nodes = []
        for k in ["HOME_DASHBOARD", "MESSAGE_LIST", "PREVIEW", "DASHBOARD", 
                  "FOLDER_TREE", "SIDEBAR", "SIDEBAR_HEADER", "CONTENT"]:
            all_settings_nodes.extend(regions.get(k, []))

        if not all_settings_nodes:
            return lines

        sidebar_nodes: List[Node] = []
        content_nodes: List[Node] = []
        split_x = 320 

        for n in all_settings_nodes:
            bbox = node_bbox_from_raw(n)
            if bbox["y"] < 50: 
                continue
            
            x = bbox["x"]
            if x <= split_x:
                sidebar_nodes.append(n)
            else:
                content_nodes.append(n)

        # ★修正: scroll_ratio を 10.0 に増やし、
        # 下の方にある項目(Work, Personal等)を deep ではなく below_fold として拾うように調整
        visible_content, below_fold_content, deep_content = self._split_by_vertical_position(
            content_nodes, fold_y, visible_ratio=1.0, scroll_ratio=10.0
        )
        # 念のため deep も結合する
        below_fold_content.extend(deep_content)

        # --- 出力構築 ---
        lines.append("=== SETTINGS ===")

        # 左サイドバー
        visible_sidebar, _, _ = self._split_by_vertical_position(sidebar_nodes, fold_y, visible_ratio=1.0)
        sidebar_lines = self._compress_settings_sidebar(visible_sidebar)
        if sidebar_lines:
            lines.append("=== SETTINGS SIDEBAR ===")
            lines.extend(sidebar_lines)

        # 画面内の設定項目
        main_lines = self._compress_settings_main(visible_content, fold_y)
        if main_lines:
            lines.append("=== SETTINGS MAIN ===")
            lines.extend(main_lines)

        # スクロール先
        below_lines = self._compress_settings_below_fold(below_fold_content)
        if below_lines:
            lines.append("=== SETTINGS (scroll down) ===")
            lines.extend(below_lines)

        return lines

    def _compress_account_settings_view(
        self,
        regions: Dict[str, List[Node]],
        modal_nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> List[str]:
        lines: List[str] = []
        fold_y = 1080

        all_nodes = []
        target_regions = [
            "HOME_DASHBOARD", "MESSAGE_LIST", "PREVIEW", "DASHBOARD", 
            "FOLDER_TREE", "SIDEBAR", "SIDEBAR_HEADER", "CONTENT", "MODAL"
        ]
        for k in target_regions:
            all_nodes.extend(regions.get(k, []))
        
        if modal_nodes:
            all_nodes.extend(modal_nodes)

        if not all_nodes:
            return lines

        sidebar_nodes: List[Node] = []
        content_nodes: List[Node] = []
        split_x = 320 

        for n in all_nodes:
            bbox = node_bbox_from_raw(n)
            if bbox["y"] < 50: 
                continue
            
            x = bbox["x"]
            if x <= split_x:
                sidebar_nodes.append(n)
            else:
                content_nodes.append(n)

        # 上下分割
        visible_content, below_fold_content, deep_content = self._split_by_vertical_position(
            content_nodes, fold_y, visible_ratio=1.0, scroll_ratio=10.0
        )
        below_fold_content.extend(deep_content)

        lines.append("=== ACCOUNT SETTINGS ===")

        # サイドバー (インデント付き)
        visible_sidebar, _, _ = self._split_by_vertical_position(sidebar_nodes, fold_y, visible_ratio=1.0)
        sidebar_lines = self._compress_account_settings_sidebar(visible_sidebar)
        if sidebar_lines:
            lines.append("=== ACCOUNT SETTINGS SIDEBAR ===")
            lines.extend(sidebar_lines)

        # メイン (マージ機能 & フィルタ付き)
        # ★変更: 専用のメイン圧縮関数を呼ぶ
        main_lines = self._compress_account_settings_main(visible_content, fold_y)
        if main_lines:
            lines.append("=== ACCOUNT SETTINGS MAIN ===")
            lines.extend(main_lines)

        # スクロール先
        below_lines = self._compress_settings_below_fold(below_fold_content)
        if below_lines:
            lines.append("=== ACCOUNT SETTINGS (scroll down) ===")
            lines.extend(below_lines)

        return lines

    
    def _compress_account_settings_sidebar(self, nodes: List[Node]) -> List[str]:
        """
        Account Settings サイドバー。
        - X座標に基づいてインデントを付与し、階層構造を可視化する。
        - ステータスバー要素が紛れ込まないようフィルタする。
        """
        if not nodes:
            return []

        VALID_TAGS = {"tree-item", "push-button", "link"}
        
        # y順、x順
        nodes = sorted(nodes, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        
        # インデント計算用の基準X座標を探す
        # 極端に左にあるものは無視して、tree-item の最小Xを探す
        tree_items_x = [node_bbox_from_raw(n)["x"] for n in nodes if (n.get("tag")=="tree-item")]
        base_x = min(tree_items_x) if tree_items_x else 0

        lines: List[str] = []
        seen = set()

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").strip()
            if not name: continue
            
            if tag not in VALID_TAGS: continue

            # ノイズ除去
            if name in {"You are currently online.", "Done"}: continue
            if node_bbox_from_raw(n)["x"] > 350: continue

            # インデント処理
            bbox = node_bbox_from_raw(n)
            # 基準からのズレを 20px 単位でインデント1個分とする（適当なヒューリスティック）
            indent_level = max(0, int((bbox["x"] - base_x) / 15))
            indent_str = "  " * indent_level

            # フォーマット
            cx, cy = bbox_to_center_tuple(bbox)
            line = f'{indent_str}[{tag}] "{name}" @ ({cx}, {cy})'
            
            if line not in seen:
                seen.add(line)
                lines.append(line)

        return lines


    def _compress_account_settings_main(self, nodes: List[Node], fold_y: int) -> List[str]:
        """
        Account Settings Main 専用圧縮。
        1. [label] と [entry/check-box] が隣接している場合、1行に結合する。
        2. "Settings" タブなどの不要なヘッダを除去する。
        """
        if not nodes:
            return []

        # 1. 不要なタブヘッダの除去
        # "Settings" という名前の section や、それに関連する Close Tab ボタンを消す
        filtered_nodes = []
        for n in nodes:
            name = (n.get("name") or "").strip()
            tag = (n.get("tag") or "").lower()
            
            # 不要なタブ (Settings)
            if tag == "section" and name == "Settings":
                continue
            # 不要な閉じるボタン (Settingsタブの近辺にあると推測される)
            # Account Settings タブよりも左(x<600くらい)にある Close Tab は消す
            if name == "Close Tab" and node_bbox_from_raw(n)["x"] < 600:
                continue
            
            filtered_nodes.append(n)

        # 2. ソート (Y優先、次にX)
        nodes = sorted(filtered_nodes, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        
        lines: List[str] = []
        skip_next = False
        
        for i, n in enumerate(nodes):
            if skip_next:
                skip_next = False
                continue
            
            bbox = node_bbox_from_raw(n)
            if bbox["y"] > fold_y: continue # 画面外は無視

            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").strip()
            
            # --- マージ処理 ---
            # 現在が Label で、次が入力欄なら結合を試みる
            if tag == "label" and i + 1 < len(nodes):
                next_n = nodes[i+1]
                next_tag = (next_n.get("tag") or "").lower()
                next_name = (next_n.get("name") or "").strip()
                next_bbox = node_bbox_from_raw(next_n)

                # Y座標が近く(行が同じ)、X座標が右側にあるか確認
                y_diff = abs(bbox["y"] - next_bbox["y"])
                if y_diff < 20 and next_bbox["x"] > bbox["x"]:
                    # 入力欄系タグなら結合
                    if next_tag in {"entry", "check-box", "combo-box", "push-button"}:
                        # 結合フォーマット: [tag] "LabelName: ValueName"
                        # 名前が重複している場合("Account Name:" と "Account Name: ...")のケア
                        final_name = next_name
                        if name.rstrip(":") not in next_name:
                            final_name = f"{name} {next_name}"
                        
                        cx, cy = bbox_to_center_tuple(next_bbox)
                        line = f'[{next_tag}] "{final_name}" @ ({cx}, {cy})'
                        lines.append(line)
                        skip_next = True # 次のノードは処理済みとする
                        continue
            
            # マージされなかった場合は通常出力
            line = self._format_node(n)
            if line:
                lines.append(line)

        return lines

    def _compress_addons_toolbar(self, nodes: List[Node]) -> List[str]:
        """
        Add-ons Manager の上部操作群を抽出する。
        例: 'Find more add-ons', 検索入力, 'Tools for all add-ons' など
        """
        if not nodes:
            return []

        def tag(n): return (n.get("tag") or "").lower()
        def nm(n):  return (n.get("name") or "").strip()
        def bbox(n): return node_bbox_from_raw(n)

        picked: List[Node] = []
        for n in nodes:
            t = tag(n)
            name = nm(n)
            if not name and t != "entry":
                continue

            b = bbox(n)

            # Add-ons 画面の「上部」っぽい範囲（必要なら微調整）
            # y=160〜320 に検索周りや見出しがいることが多い
            if b["y"] < 140 or b["y"] > 360:
                continue

            # 検索入力（nameが空でも残す）
            if t == "entry":
                picked.append(n)
                continue

            # 上部に出やすいUI要素
            if t in {"label", "heading"}:
                # 'Find more add-ons', 'Manage Your Themes' など
                picked.append(n)
                continue

            if t == "push-button":
                # 'Tools for all add-ons' / 'More Options' など
                picked.append(n)
                continue

        picked = sorted(picked, key=lambda n: (bbox(n)["y"], bbox(n)["x"]))
        # 既存の整形/重複除去ユーティリティを使う前提
        return self._dedup_lines([self._format_node(n) for n in picked])



    def _compress_addons_tabs(self, nodes: List[Node]) -> List[str]:
        """
        Add-ons Manager のタブ列（上部の 'Settings' / 'Add-ons Manager' など）を軽く要約。
        """
        if not nodes:
            return []

        picked: List[Node] = []
        for n in nodes:
            t = (n.get("tag") or "").lower()
            nm = (n.get("name") or n.get("text") or "").strip()
            bbox = node_bbox_from_raw(n)

            # タブはだいたい y=90〜150 付近にいる想定
            if bbox["y"] > 180:
                continue

            txt = ((n.get("name") or n.get("text") or "")).strip()
            if t in {"section", "label"} and txt:
                # "Settings", "Add-ons Manager", アカウント名タブなど
                picked.append(n)
            elif t == "push-button" and nm in {"Close Tab"}:
                picked.append(n)

        picked = sorted(picked, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        return self._dedup_lines([self._format_node(n) for n in picked])


    def _compress_addons_sidebar(self, nodes: List[Node]) -> List[str]:
        if not nodes:
            return []

        SIDEBAR_MAX_X = 420
        ALLOW_TAGS = {"section", "link", "list-item", "label", "heading"}
        TAG_PRIORITY = {"link": 4, "list-item": 3, "label": 2, "section": 1, "heading": 1}

        def disp(n):
            return ((n.get("name") or n.get("text") or "")).strip()

        grouped: Dict[str, List[Node]] = {}
        for n in nodes:
            t = (n.get("tag") or "").lower()
            if t not in ALLOW_TAGS:
                continue

            txt = disp(n)
            if not txt:
                continue

            bbox = node_bbox_from_raw(n)
            if bbox["x"] > SIDEBAR_MAX_X:
                continue

            key = txt.lower()  # ★重複潰しやすく
            grouped.setdefault(key, []).append(n)

        picked: List[Node] = []
        for key, group in grouped.items():
            best = sorted(
                group,
                key=lambda n: (
                    -TAG_PRIORITY.get((n.get("tag") or "").lower(), 0),
                    node_bbox_from_raw(n)["y"],
                    node_bbox_from_raw(n)["x"],
                ),
            )[0]
            picked.append(best)

        picked = sorted(picked, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        return self._dedup_lines([self._format_node(n) for n in picked])



    def _compress_addons_content(self, nodes: List[Node]) -> List[str]:
        """
        Add-ons Manager の右側コンテンツ（テーマ一覧、Enabled/Saved Themes、説明文、Enableボタン等）
        am["CONTENT"] 用。
        """
        if not nodes:
            return []

        # 右ペインっぽい領域だけを優先（左ナビと混ざるのを抑制）
        CONTENT_LEFT_X = 400

        allowed_tags = {
            "heading", "section", "link", "push-button",
            "label", "static", "paragraph", "list-item",
            "check-box", "entry",
        }

        filtered: List[Node] = []
        for n in nodes:
            bbox = node_bbox_from_raw(n)
            tg = (n.get("tag") or "").lower()
            if bbox["x"] >= CONTENT_LEFT_X and tg in allowed_tags:
                filtered.append(n)

        if not filtered:
            # もし抽出しすぎたら全体で最低限出す
            filtered = [n for n in nodes if ((n.get("tag") or "").lower() in allowed_tags)]

        # 読みやすさ：上から下、同じ段なら左から右
        filtered = sorted(filtered, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))

        lines = [self._format_node(n) for n in filtered]
        return self._dedup_lines(lines)



    def _detect_view_type(self, nodes: List[Node]) -> str:
        """
        Thunderbird view type detector (score-based + a few strong guards).

        Views:
        - "mail"
        - "home"
        - "settings"
        - "addons_manager"
        - "account_settings"
        - "compose"
        - "unknown"
        """
        from collections import defaultdict
        from typing import Dict

        def tag(n): return (n.get("tag") or "").lower()
        def name(n): return (n.get("name") or "").strip()
        def lower_name(n): return name(n).lower()

        # ★ 文字列は disp/ldisp に統一して判定する（name空/text側対応）
        def disp(n):
            return ((n.get("name") or n.get("text") or n.get("description") or "")).strip()
        def ldisp(n): return disp(n).lower()

        # ----------------------------
        # 1) STRONG GUARDS (確定ルール)
        # ----------------------------

        # --- Compose (New Message) guard ---
        # "Message body" + (From/To/Subject/Send etc.) の組み合わせで強確定
        has_message_body = any(
            tag(n) == "document-web" and ldisp(n) in {"message body", "body"}
            for n in nodes
        )

        compose_signals = {
            "from", "to", "subject", "cc", "bcc",
            "send", "attach", "spelling",
        }
        compose_hits = sum(
            1 for n in nodes
            if tag(n) in {"label", "entry", "push-button", "combo-box", "toggle-button"}
            and ldisp(n) in compose_signals
        )

        # "Subject" は entry の name が "Subject"、text に件名が入る等の揺れがあるので補強
        has_subject_entry = any(
            tag(n) == "entry" and (ldisp(n) == "subject" or lower_name(n) == "subject")
            for n in nodes
        )

        # 2-of-N 以上で確定（強め）
        if has_message_body and (compose_hits >= 2 or has_subject_entry):
            return "compose"

        # --- Add-ons Manager guard ---
        # NOTE: document-web の title が必ず "Add-ons Manager" とは限らないので、
        # Add-ons 特有の UI（検索欄 / 見出し / ツールボタン）も確定材料にする。
        is_addons_guard = (
            any(tag(n) == "document-web" and "add-ons" in ldisp(n) for n in nodes)
            or any(tag(n) == "entry" and "addons.thunderbird.net" in ldisp(n) for n in nodes)
            or any(tag(n) == "label" and ldisp(n) == "find more add-ons" for n in nodes)
            or any(tag(n) == "heading" and ldisp(n) in {"manage your themes", "manage your extensions"} for n in nodes)
            or any(tag(n) == "push-button" and ldisp(n) == "tools for all add-ons" for n in nodes)
        )
        if is_addons_guard:
            return "addons_manager"

        # --- Account Settings guard (keep yours) ---
        if any("account settings -" in ldisp(n) for n in nodes):
            return "account_settings"

        # ----------------------------
        # 2) SCORE-BASED (柔らかい判定)
        # ----------------------------
        score: Dict[str, float] = defaultdict(float)

        # --- mail signals ---
        mail_keywords = {"quick filter", "message list display options"}
        if any(ldisp(n) in mail_keywords for n in nodes):
            score["mail"] += 3

        # message rows: tree-item にカンマ含む行が複数ある → メール一覧っぽい
        msg_row_hits = sum(1 for n in nodes if tag(n) == "tree-item" and "," in disp(n))
        if msg_row_hits >= 2:
            score["mail"] += 3
        elif msg_row_hits == 1:
            score["mail"] += 1

        # --- home signals ---
        home_headings = {
            "set up another account",
            "import from another program",
            "about mozilla thunderbird",
            "resources",
        }
        home_heading_hits = sum(
            1 for n in nodes
            if tag(n) == "heading" and ldisp(n) in home_headings
        )
        score["home"] += min(home_heading_hits * 2, 6)

        # --- settings signals ---
        # Settings本体 (document-web=Settings) は強シグナル
        if any(tag(n) == "document-web" and ldisp(n) == "settings" for n in nodes):
            score["settings"] += 6

        # 左ナビ (Settings) は Add-ons 画面にも出るので加点を控えめにする
        settings_nav = {"general", "composition", "privacy & security", "chat"}
        nav_hits = sum(
            1 for n in nodes
            if tag(n) in {"list-item", "label"} and ldisp(n) in settings_nav
        )
        score["settings"] += min(nav_hits, 2)

        # タブ名などで "Settings" セクションがある
        if any(tag(n) == "section" and ldisp(n) == "settings" for n in nodes):
            score["settings"] += 2

        # --- addons signals (guardに落ちなかった時の保険) ---
        if any(tag(n) == "section" and "add-ons manager" in ldisp(n) for n in nodes):
            score["addons_manager"] += 4
        if any(tag(n) == "document-web" and "add-ons manager" in ldisp(n) for n in nodes):
            score["addons_manager"] += 4

        if any(tag(n) == "entry" and "addons.thunderbird.net" in ldisp(n) for n in nodes):
            score["addons_manager"] += 6
        if any(tag(n) == "label" and ldisp(n) == "find more add-ons" for n in nodes):
            score["addons_manager"] += 3
        if any(tag(n) == "heading" and ldisp(n) == "manage your themes" for n in nodes):
            score["addons_manager"] += 3
        if any(tag(n) == "push-button" and ldisp(n) == "tools for all add-ons" for n in nodes):
            score["addons_manager"] += 3

        addons_nav = {"recommendations", "extensions", "themes", "languages"}
        addons_hits = sum(
            1 for n in nodes
            if tag(n) in {"section", "list-item", "label", "link"}
            and ldisp(n) in addons_nav
        )
        score["addons_manager"] += min(addons_hits, 4)

        # --- compose signals (guardに落ちなかった時の保険) ---
        # ガードほど強くないが、それっぽさを加点
        if any(tag(n) == "document-web" and ldisp(n) in {"message body", "body"} for n in nodes):
            score["compose"] += 6
        # フィールド類が複数あると compose っぽい
        compose_field_keys = {"from", "to", "subject", "cc", "bcc"}
        compose_field_hits = sum(
            1 for n in nodes
            if tag(n) in {"label", "entry", "combo-box"} and ldisp(n) in compose_field_keys
        )
        score["compose"] += min(compose_field_hits, 4)
        if any(tag(n) == "push-button" and ldisp(n) == "send" for n in nodes):
            score["compose"] += 2
        if any(tag(n) == "push-button" and ldisp(n) == "attach" for n in nodes):
            score["compose"] += 1

        # ----------------------------
        # 3) 決定ロジック
        # ----------------------------
        MIN_SCORE = 3.0

        top = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
        if not top or top[0][1] < MIN_SCORE:
            return "unknown"

        # 競合時のマージン
        if len(top) >= 2:
            v1, s1 = top[0]
            v2, s2 = top[1]

            # settings vs addons_manager は誤判定が痛いので厳しめに
            margin = 2.0 if {v1, v2} == {"settings", "addons_manager"} else 0.5
            if (s1 - s2) < margin:
                return "unknown"

        return top[0][0]


    def _estimate_split_msg_list_x(self, nodes, fallback=1040):
        """
        3-pane の境界 x を動的推定する。
        アイデア: 主要ノードの x を並べ、最大ギャップの中点を境界とみなす。
        """
        xs = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if not tag:
                continue
            bbox = node_bbox_from_raw(n)
            x, y = bbox["x"], bbox["y"]

            # Launcher/Spaces（超左）やメニューバーっぽい領域を除外
            if x < 200:
                continue

            # 極端に上（メニューバー）も境界推定にはノイズになりやすいので軽く除外
            if y < 40:
                continue

            xs.append(x)

        if len(xs) < 10:
            return fallback

        xs.sort()

        # 最大ギャップの場所を探す
        best_gap = 0
        best_mid = None
        MIN_VALID_GAP = 120  # 3-pane の分断として妥当な最小幅

        for a, b in zip(xs, xs[1:]):
            gap = b - a
            if gap > best_gap and gap >= MIN_VALID_GAP:
                best_gap = gap
                best_mid = (a + b) / 2

        if best_mid is None:
            return fallback

        # あり得ない境界を弾く（環境差の暴走防止）
        # Thunderbird の一般的な 3-pane はだいたい 800〜1400 の間に境界が来ることが多い
        if not (800 <= best_mid <= 1400):
            return fallback

        return int(best_mid)


    def _estimate_msg_list_header_cut_y(self, nodes, msg_left_x, split_x, fallback=140):
        """
        ヘッダ境界 y を推定する。
        アイデア: メール一覧領域の 'tree-item' の最小 y（最初の行）からヘッダ終端を逆算。
        """
        item_ys = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag != "tree-item":
                continue
            bbox = node_bbox_from_raw(n)
            x, y = bbox["x"], bbox["y"]

            # メール一覧領域だけ見る（左ペイン）
            if msg_left_x < x < split_x:
                # あまり上すぎる tree-item（フォルダツリー）を避けたいなら y>=100 など
                if y >= 80:
                    item_ys.append(y)

        if not item_ys:
            return fallback

        first_y = min(item_ys)
        cut = int(first_y - 5)  # 少しだけ上にずらす

        # 暴走防止（上すぎ/下すぎをクランプ）
        if cut < 90:
            cut = 90
        if cut > 220:
            cut = 220

        return cut





    def _build_mail_view(self, regions: Dict[str, List[Node]]) -> List[str]:
        """
        Thunderbird の Mail View（3-Pane レイアウト）専用の圧縮出力を作る。
        SYSTEM / LAUNCHER / TOOLBAR / TOP_BAR / SPACES などの共通部分は
        _build_output() 側で出力するため、ここでは出力しない。
        """
        lines = []

        # ---------------------------------------------------------
        # 1) FOLDER_LIST（左ペイン）
        # ---------------------------------------------------------
        folder_nodes = regions["FOLDER_TREE"] + regions["SIDEBAR_HEADER"] + regions["SIDEBAR"]
        folder_list = self._compress_folder_tree(folder_nodes)

        if folder_list:
            lines.append("=== FOLDER_LIST ===")
            lines.extend(folder_list)
            lines.append("")

        # ---------------------------------------------------------
        # 2) メール一覧＋右側ペインを 3 分割する
        # ---------------------------------------------------------
        candidates = regions["MESSAGE_LIST"] + regions["PREVIEW"] + regions["MAIL_TOOLBAR"]
        MSG_LIST_LEFT_X = 340 

        # ★ここが変更点：固定値ではなく推定
        SPLIT_MSG_LIST_X = self._estimate_split_msg_list_x(candidates, fallback=1040)
        HEADER_CUT_Y = self._estimate_msg_list_header_cut_y(candidates, MSG_LIST_LEFT_X, SPLIT_MSG_LIST_X, fallback=140)

        msg_list_header = []
        msg_list_items = []
        msg_actions = []
        msg_header = []
        msg_body = []

        for n in candidates:
            bbox = node_bbox_from_raw(n)
            x, y = bbox["x"], bbox["y"]
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").strip()

            # ============================
            # 左 〈メール一覧〉 message list
            # ============================
            if MSG_LIST_LEFT_X < x < SPLIT_MSG_LIST_X:
                if y < HEADER_CUT_Y:
                    msg_list_header.append(n)
                    # 任意：デバッグ
                    # print(f"[DEBUG-ML->HEADER] {tag} {name[:40]} @ ({x:.1f},{y:.1f})")
                else:
                    msg_list_items.append(n)
                    # print(f"[DEBUG-ML->ITEMS] {tag} {name[:40]} @ ({x:.1f},{y:.1f})")

            # ============================
            # 右 〈メール閲覧〉 reading pane
            # ============================
            elif x >= SPLIT_MSG_LIST_X:
                # 上段（Replyなど）
                if y < HEADER_CUT_Y:
                    msg_actions.append(n)
                    continue

                # 下段（本文）
                if tag == "document-web" or y > 260:
                    msg_body.append(n)
                    continue

                # 中段（From/To/Subject）
                msg_header.append(n)


        # ---------------------------------------------------------
        # 3) MESSAGE_LIST_HEADER
        # ---------------------------------------------------------
        if msg_list_header:
            lines.append("=== MESSAGE_LIST_HEADER ===")
            msg_list_header.sort(key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
            for n in msg_list_header:
                lines.append(self._format_node(n))
            lines.append("")

        # ---------------------------------------------------------
        # 4) MESSAGE_LIST（メール一覧）
        # ---------------------------------------------------------
        if msg_list_items:
            lines.append("=== MESSAGE_LIST ===")

            items = [n for n in msg_list_items]
            items.sort(key=lambda n: node_bbox_from_raw(n)["y"])

            seen_list = set()
            for n in items:
                raw_name = (n.get("name") or "").strip()
                if not raw_name:
                    continue
                tag = (n.get("tag") or "").lower()
                formatted = raw_name.replace(", ", " — ") if tag == "tree-item" else raw_name

                n_copy = dict(n)
                n_copy["name"] = formatted

                l = self._format_node(n_copy)
                if l and l not in seen_list:
                    seen_list.add(l)
                    lines.append(l)
            lines.append("")

        # ---------------------------------------------------------
        # 5) MESSAGE_ACTIONS（Reply / Forward / Delete）
        # ---------------------------------------------------------
        if msg_actions:
            lines.append("=== MESSAGE_ACTIONS ===")
            msg_actions.sort(key=lambda n: node_bbox_from_raw(n)["x"])
            for n in msg_actions:
                lines.append(self._format_node(n))
            lines.append("")

        # ---------------------------------------------------------
        # 6) MESSAGE_HEADER（From / To / Subject）
        # ---------------------------------------------------------
        if msg_header:
            lines.append("=== MESSAGE_HEADER ===")

            msg_header.sort(key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))

            seen_hdr = set()
            for n in msg_header:
                tag = (n.get("tag") or "").lower()
                name = (n.get("name") or "").strip()
                if not name:
                    continue

                # 不要な image / section は軽く除外
                if tag in {"image", "section"} and not name:
                    continue

                l = self._format_node(n)
                if l and l not in seen_hdr:
                    seen_hdr.add(l)
                    lines.append(l)

            lines.append("")

        # ---------------------------------------------------------
        # 7) MESSAGE_BODY（本文）
        # ---------------------------------------------------------
        if msg_body:
            lines.append("=== MESSAGE_BODY ===")
            msg_body.sort(key=lambda n: node_bbox_from_raw(n)["y"])

            for n in msg_body:
                name = (n.get("name") or "").strip()
                tag = (n.get("tag") or "").lower()

                # ノードに本文が存在する場合は “そのまま全文出力”
                if name:
                    lines.append(name)
                else:
                    # 名前が無いノード（画像、リンクなど）は最低限フォーマットして出力
                    lines.append(self._format_node(n))

            lines.append("")

        # ---------------------------------------------------------
        # 8) STATUSBAR（共通）
        # ---------------------------------------------------------
        if r := self._compress_statusbar(regions["STATUSBAR"]):
            lines.append("=== STATUSBAR ===")
            lines.extend(r)
            lines.append("")

        return lines

    def _dedup_nodes(self, nodes: List[Node]) -> List[Node]:
        seen = set()
        out = []
        for n in nodes:
            key = (
                (n.get("tag") or ""),
                (n.get("name") or ""),
                tuple(n.get("position") or (None, None)),
                tuple(n.get("size") or (None, None)),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(n)
        return out

    def _dedup_lines(self, lines: List[str]) -> List[str]:
        seen = set()
        out = []
        for s in lines:
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out



    def _build_addons_manager_view(
        self,
        regions: dict,
        modal_nodes_for_output: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> dict:
        import sys  # DEBUG用

        # helper
        def tag(n): return (n.get("tag") or "").lower()
        def disp(n): return ((n.get("name") or n.get("text") or n.get("description") or "")).strip()
        def ldisp(n): return disp(n).lower()
        def xy(n):
            b = node_bbox_from_raw(n)
            return (b["x"], b["y"])


        # ----------------------------------------
        # 0) “Add-ons Manager の中身候補”だけ集める
        # ----------------------------------------
        candidate_nodes: List[Node] = []

        # 0-1) 最優先：呼び出し側が渡してきた modal candidates
        if modal_nodes_for_output:
            candidate_nodes.extend(modal_nodes_for_output)
        print("[DEBUG-AM] modal_nodes_for_output:", len(modal_nodes_for_output), file=sys.stderr, flush=True)

        # 0-2) 補完：regions からも拾う（MODAL を “除外しない”）
        EXCLUDE_KEYS = {"APP_LAUNCHER", "SPACES_BAR", "TOOLBAR", "TOP_BAR", "STATUSBAR"}
        for k, lst in (regions or {}).items():
            if not lst or k in EXCLUDE_KEYS:
                continue
            candidate_nodes.extend(lst)
        print("[DEBUG-AM] candidate_nodes before x-filter:", len(candidate_nodes), file=sys.stderr, flush=True)
        for n in candidate_nodes:
            s = ldisp(n)
            if s in {"recommendations", "extensions", "themes", "languages"}:
                pos = n.get("position")
                b = node_bbox_from_raw(n)
                print("[CHECK-NAV]", s, "pos=", pos, "bbox=", (b["x"], b["y"]), "tag=", tag(n),
                    file=sys.stderr, flush=True)

        # 0-3) 左のランチャー縦バーを除外（position欠損は落とさない）
        filtered_candidates: List[Node] = []
        for n in candidate_nodes:
            pos = n.get("position")
            if not pos:
                filtered_candidates.append(n)
                continue
            x, y = pos
            if (x or 0) < 85:
                continue
            filtered_candidates.append(n)
        candidate_nodes = filtered_candidates
        print("[DEBUG-AM] candidate_nodes after x-filter:", len(candidate_nodes), file=sys.stderr, flush=True)

        # ----------------------------------------
        # 1) しきい値
        # ----------------------------------------
        LEFT_NAV_X_MAX = 420
        TAB_Y_MAX = 155

        tabs: List[Node] = []
        sidenav: List[Node] = []
        addons_toolbar: List[Node] = []
        content: List[Node] = []

        # keywords
        nav_keys = {
            "recommendations", "extensions", "themes", "languages",
            "add-ons support", "thunderbird settings",
        }
        tab_keys = {"settings", "add-ons manager", "close tab"}
        toolbar_keys = {
            "search addons.thunderbird.net",
            "tools for all add-ons",
            "find more add-ons",
        }

        # ----------------------------------------
        # 2) 分類
        # ----------------------------------------
        for n in candidate_nodes:
            x, y = xy(n)
            t = tag(n)
            s = ldisp(n)

            # document-web は「ページの根」なので content には落とさない（ノイズ対策）
            if t == "document-web":
                continue

            # --- Tabs area ---
            if y <= TAB_Y_MAX and t in {"section", "push-button"}:
                if any(k in s for k in tab_keys):
                    tabs.append(n)
                    continue

            # --- Left navigation ---
            if x <= LEFT_NAV_X_MAX and t in {"list-item", "link", "section", "label"}:
                if any(k in s for k in nav_keys):
                    sidenav.append(n)
                    continue

            # --- Add-ons toolbar-ish ---
            if t in {"entry", "push-button", "label"}:
                if any(k in s for k in toolbar_keys):
                    addons_toolbar.append(n)
                    continue

            # --- Others -> content ---
            content.append(n)

        # ----------------------------------------
        # 3) dedup
        # ----------------------------------------
        tabs = self._dedup_nodes(tabs)
        addons_toolbar = self._dedup_nodes(addons_toolbar)
        content = self._dedup_nodes(content)

        # ★ SIDENAV は「同一ラベルが link/list-item/section で多重に出る」ので文字列ベースで追加dedup
        #sidenav = self._dedup_nodes(sidenav)
        def xy_bbox(n):
            b = node_bbox_from_raw(n)
            return b["x"], b["y"]
            
        seen_nav_text = set()
        sidenav2: List[Node] = []
        for n in sorted(sidenav, key=lambda n: (xy(n)[1], xy(n)[0])):  # 上から順に
            text = ldisp(n)
            if not text:
                continue
            if text in seen_nav_text:
                continue
            seen_nav_text.add(text)
            sidenav2.append(n)
        sidenav = sidenav2

        return {
            "TABS": tabs,
            "SIDENAV": sidenav,
            "ADDONS_TOOLBAR": addons_toolbar,
            "CONTENT": content,
        }



    
    def _is_inside_mail_area(self, node: Node, mail_area_nodes: List[Node]) -> bool:
        """
        modal_nodes のうち「メール本文エリア上に出ているものか」を判定する。
        ★注意: node["bounds"] ではなく node_bbox_from_raw() を使う。
        """
        if not mail_area_nodes:
            return False

        xs: List[float] = []
        ys: List[float] = []
        xe: List[float] = []
        ye: List[float] = []

        # メール本文エリアの外接矩形を作る
        for n in mail_area_nodes:
            bbox = node_bbox_from_raw(n)
            bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            xs.append(bx)
            ys.append(by)
            xe.append(bx + bw)
            ye.append(by + bh)

        min_x, max_x = min(xs), max(xe)
        min_y, max_y = min(ys), max(ye)

        # 判定対象ノードの中心座標
        bbox = node_bbox_from_raw(node)
        bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
        cx = bx + bw / 2.0
        cy = by + bh / 2.0

        margin = 10  # 少し余白を持たせる
        return (
            (min_x - margin) <= cx <= (max_x + margin)
            and (min_y - margin) <= cy <= (max_y + margin)
        )



    def _reclassify_false_modals_in_mail(self, regions: Dict[str, List[Node]], modal_nodes_for_output: List[Node]) -> List[Node]:
        """
        MAIL view では、diff modal が誤って MESSAGE_LIST 周辺を MODAL に入れることがある。
        その場合、3-pane の座標で背景領域へ戻す。
        """
        SPLIT_MSG_LIST_X = 1040
        LEFT_FOLDER_X = 380   # まずは 380〜400 あたり。ログ見る限り 380 が境界近い
        TOP_Y = 160           # Quick Filter が y=127 なので、header帯は 160 くらいが安全

        # これらは mail UI で普通に出るので modal 扱いしない
        safe_tags = {"toggle-button", "push-button", "heading", "section", "tree-item", "list-item", "static", "label"}

        def move_to_background(n: Node) -> None:
            bbox = node_bbox_from_raw(n)
            x, y = bbox["x"], bbox["y"]
            tag = (n.get("tag") or "").lower()

            # 上部帯 → MAIL_TOOLBAR（Quick Filter等）
            if y < TOP_Y:
                regions.setdefault("MAIL_TOOLBAR", []).append(n)
                return

            # 左ペイン
            if x < LEFT_FOLDER_X:
                regions.setdefault("FOLDER_TREE", []).append(n)
                return

            # 中央（メール一覧）
            if x < SPLIT_MSG_LIST_X:
                regions.setdefault("MESSAGE_LIST", []).append(n)
                return

            # 右（閲覧ペイン）
            regions.setdefault("PREVIEW", []).append(n)

        # regions["MODAL"] の誤モーダルを戻す
        new_modal_region: List[Node] = []
        for n in regions.get("MODAL", []):
            tag = (n.get("tag") or "").lower()
            if tag in safe_tags:
                move_to_background(n)
            else:
                new_modal_region.append(n)
        regions["MODAL"] = new_modal_region

        # diff由来 modal_nodes_for_output の誤モーダルも戻す
        kept: List[Node] = []
        for n in (modal_nodes_for_output or []):
            tag = (n.get("tag") or "").lower()
            if tag in safe_tags:
                move_to_background(n)
            else:
                kept.append(n)

        return kept


    def _rescue_message_list_from_modal(
        self,
        regions: Dict[str, List[Node]],
        modal_nodes_for_output: List[Node],
        msg_left_x: int,
        split_x: int,
        header_cut_y: int,
    ):
        """
        Mail view で MESSAGE_LIST が MODAL に落ちる事故を救済する。
        - regions["MODAL"] と diff由来 modal_nodes_for_output の両方から救済
        - 左ペイン（message list）領域にあるノードを rescued として返す
        戻り値:
            kept_modal_nodes_for_output, rescued_msg_list_nodes
        """
        rescued: List[Node] = []
        kept_modal_out: List[Node] = []

        # 左ペイン判定に使う tag セット（header/rows/操作部品を広めに許容）
        MSG_TAGS = {
            "tree-item",
            "list-item",
            "section",
            "heading",
            "toggle-button",
            "push-button",
            "check-box",
            "entry",
            "label",
            "static",
        }

        def is_left_pane_msg_list_node(n: Node) -> bool:
            bbox = node_bbox_from_raw(n)
            x, y = bbox["x"], bbox["y"]
            tag = (n.get("tag") or "").lower()

            if tag not in MSG_TAGS:
                return False

            # MESSAGE_LIST はだいたい x が msg_left_x〜split_x に収まる
            if not (msg_left_x <= x < split_x):
                return False

            # header(例: Inbox / 2 Messages / Quick Filter) は y が小さい
            # rows は y が header_cut_y より少し下〜数百pxの範囲
            if y < 0:
                return False

            # ざっくり上側に寄っているものを message list とみなす（保守的に）
            if y <= (header_cut_y + 600):
                return True

            return False

        # (A) regions["MODAL"] から救済して、MODAL を更新
        new_modal_region: List[Node] = []
        for n in regions.get("MODAL", []):
            if is_left_pane_msg_list_node(n):
                rescued.append(n)
            else:
                new_modal_region.append(n)
        regions["MODAL"] = new_modal_region

        # (B) diff由来 modal_nodes_for_output から救済
        for n in (modal_nodes_for_output or []):
            if is_left_pane_msg_list_node(n):
                rescued.append(n)
            else:
                kept_modal_out.append(n)

        # 重複排除（同一オブジェクトID）
        seen = set()
        uniq_rescued: List[Node] = []
        for n in rescued:
            nid = id(n)
            if nid not in seen:
                seen.add(nid)
                uniq_rescued.append(n)

        return kept_modal_out, uniq_rescued


    def _build_compose_view(
        self,
        regions: dict,
        modal_nodes_for_output: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> dict:
        import sys

        def tag(n): return (n.get("tag") or "").lower()
        def disp(n): return ((n.get("name") or n.get("text") or n.get("description") or "")).strip()
        def ldisp(n): return disp(n).lower()
        def bbox(n): return node_bbox_from_raw(n)

        # 0) composeの候補を集める
        candidate_nodes: List[Node] = []

        if modal_nodes_for_output:
            candidate_nodes.extend(modal_nodes_for_output)

        EXCLUDE_KEYS = {
            "APP_LAUNCHER", "SPACES_BAR", "TOOLBAR", "TOP_BAR", "STATUSBAR",
            "WINDOW_CONTROLS",
        }
        for k, lst in (regions or {}).items():
            if not lst or k in EXCLUDE_KEYS:
                continue
            candidate_nodes.extend(lst)

        # 1) ランチャー縦バー誤爆を避けたいので bbox で除外（x<85）
        filtered: List[Node] = []
        for n in candidate_nodes:
            b = bbox(n)
            if b["x"] < 85:
                continue
            filtered.append(n)
        candidate_nodes = filtered

        # 2) composeを領域分割
        menubar: List[Node] = []
        actions: List[Node] = []
        fields: List[Node] = []
        formatting: List[Node] = []
        body: List[Node] = []

        # しきい値（あなたの例の座標に合わせた初期値）
        MENUBAR_Y_MAX = 160     # menu(File/Edit/...)
        ACTIONS_Y_MAX = 210     # Send/Attach/Save/Spellingあたり
        FIELDS_Y_MAX = 320      # From/To/Subject
        FORMAT_Y_MAX = 350      # 太字/箇条書きなどの整形バー
        BODY_Y_MIN = 340        # document-web "Message body" + paragraph群

        # キーワード
        action_keys = {"send", "attach", "save", "spelling", "contacts"}
        field_keys = {"from", "to", "subject", "cc", "bcc"}

        for n in candidate_nodes:
            t = tag(n)
            s = ldisp(n)
            b = bbox(n)
            y = b["y"]

            # document-web "Message body" は BODY へ
            if t == "document-web" and s in {"message body", "body"}:
                body.append(n)
                continue

            # 1) Menubar
            if t == "menu" and y <= MENUBAR_Y_MAX:
                menubar.append(n)
                continue

            # 2) Actions row (Send/Attach etc.)
            if t in {"push-button", "toggle-button"} and y <= ACTIONS_Y_MAX:
                if s in action_keys:
                    actions.append(n)
                    continue

            # 3) Fields (From/To/Subject + entry/combo-box + label)
            if y <= FIELDS_Y_MAX:
                if (t in {"label", "entry", "combo-box", "push-button"} and (s in field_keys or "bcc" == s or "cc" == s)):
                    fields.append(n)
                    continue
                # From/To/Subject の entry が長文になるケースもあるので補強
                if t in {"entry", "combo-box"} and any(k in s for k in ("from", "to", "subject")):
                    fields.append(n)
                    continue

            # 4) Formatting toolbar
            if y <= FORMAT_Y_MAX:
                if t in {"push-button", "toggle-button", "combo-box", "menu-item"}:
                    formatting.append(n)
                    continue

            # 5) Body paragraphs / etc.
            if y >= BODY_Y_MIN:
                if t in {"paragraph", "section", "static", "label", "link"}:
                    body.append(n)
                    continue

        # 3) dedup（tabsでやったのと同じノリ）
        menubar = self._dedup_nodes(menubar)
        actions = self._dedup_nodes(actions)
        fields = self._dedup_nodes(fields)
        formatting = self._dedup_nodes(formatting)
        # body = self._dedup_nodes(body)
        body = body

        return {
            "MENUBAR": menubar,
            "ACTIONS": actions,
            "FIELDS": fields,
            "FORMATTING": formatting,
            "BODY": body,
        }


    def _compress_menubar(self, nodes: List[Node]) -> List[str]:
        if not nodes:
            return []
        nodes = sorted(nodes, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        return self._dedup_lines([self._format_node(n) for n in nodes])


    def _compress_compose_actions(self, nodes: List[Node]) -> List[str]:
        if not nodes:
            return []
        # 重要ボタンだけ優先表示
        priority = {"send": 0, "attach": 1, "save": 2, "spelling": 3, "contacts": 4}
        def key(n):
            s = ((n.get("name") or n.get("text") or "")).strip().lower()
            b = node_bbox_from_raw(n)
            return (priority.get(s, 99), b["y"], b["x"])
        nodes = sorted(nodes, key=key)
        return self._dedup_lines([self._format_node(n) for n in nodes])


    def _compress_compose_fields(self, nodes: List[Node]) -> List[str]:
        if not nodes:
            return []

        def t(n): return (n.get("tag") or "").lower()
        def d(n): return ((n.get("name") or n.get("text") or "")).strip()
        def ld(n): return d(n).lower()
        def b(n): return node_bbox_from_raw(n)

        items = sorted(nodes, key=lambda n: (b(n)["y"], b(n)["x"]))

        # label候補
        labels = [n for n in items if t(n) == "label" and ld(n) in {"from", "to", "subject"}]
        # 入力候補
        inputs = [n for n in items if t(n) in {"entry", "combo-box"}]

        # yが近い入力を対応付け
        pairs = []
        used = set()
        for lab in labels:
            ly = b(lab)["y"]
            best = None
            best_dist = 1e9
            for inp in inputs:
                if id(inp) in used:
                    continue
                iy = b(inp)["y"]
                dist = abs(iy - ly)
                if dist < best_dist:
                    best, best_dist = inp, dist
            if best is not None and best_dist <= 20:  # 近接しきい値
                used.add(id(best))
                pairs.append((lab, best))
            else:
                pairs.append((lab, None))

        lines = []
        for lab, inp in pairs:
            if inp is None:
                lines.append(self._format_node(lab))
            else:
                # Subject entry は text に値が入るので format_node が効く
                lines.append(f"{self._format_node(lab)}  ->  {self._format_node(inp)}")

        # CC/BCC はボタン/ラベルが混ざるので残りを追記
        leftovers = [n for n in items if id(n) not in used and n not in labels]
        # 重要そうなものだけ（Cc/Bcc + アドレス表示）
        keep = []
        for n in leftovers:
            s = ld(n)
            if s in {"cc", "bcc"} or "@" in s or t(n) in {"entry", "combo-box"}:
                keep.append(n)

        keep = self._dedup_nodes(keep)
        keep = sorted(keep, key=lambda n: (b(n)["y"], b(n)["x"]))
        lines.extend([self._format_node(n) for n in keep])

        return self._dedup_lines(lines)

    def _compress_compose_formatting(self, nodes: List[Node]) -> List[str]:
        if not nodes:
            return []
        # ここは多いので、押せる系＋combo-boxだけに絞る
        allowed = {"push-button", "toggle-button", "combo-box"}
        filtered = [n for n in nodes if ((n.get("tag") or "").lower() in allowed)]
        filtered = self._dedup_nodes(filtered)
        filtered = sorted(filtered, key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        return self._dedup_lines([self._format_node(n) for n in filtered])


    def _compress_compose_body(self, nodes: List[Node]) -> List[str]:
        if not nodes:
            return []

        def tg(n): return (n.get("tag") or "").lower()
        def b(n): return node_bbox_from_raw(n)
        def disp(n): return ((n.get("name") or n.get("text") or n.get("description") or "")).strip()

        # document-web / paragraph を優先して残す
        # paragraph が空文字なら落とす
        keep = []
        for n in nodes:
            if tg(n) == "document-web":
                keep.append(n)
            elif tg(n) == "paragraph":
                if disp(n):
                    keep.append(n)

        keep = sorted(keep, key=lambda n: (b(n)["y"], b(n)["x"]))

        # ★ BODY だけは dedup を強くかけない（文章が消える事故防止）
        # 代わりに “完全一致の重複” だけ落とす
        seen = set()
        out = []
        for n in keep:
            key = (tg(n), disp(n), b(n)["x"], b(n)["y"])
            if key in seen:
                continue
            seen.add(key)
            out.append(self._format_node(n))
        return out





    def _build_output(self, regions, modal_nodes, screen_w, screen_h):
        import os, sys
        print("[DEBUG] thunderbird.py path =", os.path.abspath(__file__), file=sys.stderr, flush=True)
        print("[DEBUG] _build_output called", file=sys.stderr, flush=True)

        lines: List[str] = []

        # --- view type detect ---
        all_nodes_for_detect: List[Node] = []
        for lst in regions.values():
            all_nodes_for_detect.extend(lst)
        if modal_nodes:
            all_nodes_for_detect.extend(modal_nodes)

        view_type = self._detect_view_type(all_nodes_for_detect)

        lines.append(f"DEBUG_VIEW_TYPE: {view_type}")
        print(f"[DEBUG] VIEW_TYPE = {view_type}", file=sys.stderr, flush=True)


        # ----------------------------------------
        # A案: view切替時はMODAL検知を無効化（クールダウン）
        # ----------------------------------------
        prev_view = getattr(self, "_prev_view_type", None)
        cooldown = getattr(self, "_view_change_cooldown", 0)

        # viewが変わったらクールダウン開始（1〜2推奨、まずは2）
        if prev_view is not None and view_type != prev_view:
            cooldown = 2

        # diff 由来のモーダル候補はここで管理
        modal_nodes_for_output: List[Node] = list(modal_nodes or [])


        suppressed_modal_candidates: List[Node] = []
        # クールダウン中は modal を “出力対象から除外”
        if cooldown > 0:
            # 今ある MODAL/差分modal を退避
            if regions.get("MODAL"):
                suppressed_modal_candidates.extend(regions["MODAL"])
            if modal_nodes_for_output:
                suppressed_modal_candidates.extend(modal_nodes_for_output)

            # MODALとしては出さない
            regions["MODAL"] = []
            modal_nodes = []
            modal_nodes_for_output = []

            lines.append(f"DEBUG_MODAL_SUPPRESSED: cooldown={cooldown}, prev={prev_view}, curr={view_type}")
            print(f"[DEBUG] MODAL_SUPPRESSED cooldown={cooldown} prev={prev_view} curr={view_type}")

        setattr(self, "_prev_view_type", view_type)
        setattr(self, "_view_change_cooldown", max(0, cooldown - 1))



        # ----------------------------------------
        # 1) 共通部分 (SYSTEM / LAUNCHER / TOOLBAR / SPACES)
        # ----------------------------------------
        if regions.get("TOP_BAR"):
            r = self._compress_top_bar(regions["TOP_BAR"])
            if r:
                lines.append("=== SYSTEM ===")
                lines.extend(r)
                lines.append("")

        if r := self._compress_app_launcher(regions["APP_LAUNCHER"]):
            lines.append("=== LAUNCHER ===")
            lines.extend(r)
            lines.append("")

        if r := self._compress_toolbar(regions["TOOLBAR"]):
            lines.append("=== TOOLBAR ===")
            lines.extend(r)
            lines.append("")

        if r := self._compress_spaces_bar(regions["SPACES_BAR"]):
            lines.append("=== SPACES ===")
            lines.extend(r)
            lines.append("")

        # ----------------------------------------
        # 2) メインコンテンツ (View Type ごと)
        # ----------------------------------------
        if view_type == "home":
            if r := self._compress_folder_tree(
                regions["FOLDER_TREE"] + regions["SIDEBAR_HEADER"] + regions["SIDEBAR"]
            ):
                lines.append("=== FOLDERS ===")
                lines.extend(r)
                lines.append("")

            if r := self._compress_home_dashboard(regions["HOME_DASHBOARD"] + regions["DASHBOARD"]):
                lines.append("=== HOME DASHBOARD ===")
                lines.extend(r)
                lines.append("")

        elif view_type == "settings":
            settings_lines = self._compress_settings_view(regions, screen_w, screen_h)
            if settings_lines:
                lines.extend(settings_lines)
            else:
                if r := self._compress_home_dashboard(regions["HOME_DASHBOARD"]):
                    lines.append("=== SETTINGS (Fallback) ===")
                    lines.extend(r)


        elif view_type == "account_settings":
            account_modal_nodes: List[Node] = list(modal_nodes_for_output)
            modal_nodes_for_output = []

            acc_lines = self._compress_account_settings_view(
                regions,
                account_modal_nodes,
                screen_w,
                screen_h,
            )
            if acc_lines:
                lines.extend(acc_lines)

            regions["MODAL"] = []
            
        elif view_type == "addons_manager":
            print(
                f"[DEBUG-AM] enter addons_manager: "
                f"cooldown={cooldown} prev={prev_view} curr={view_type} "
                f"regions.MODAL={len(regions.get('MODAL', []))} "
                f"modal_nodes_for_output={len(modal_nodes_for_output)} "
                f"suppressed_modal_candidates={len(suppressed_modal_candidates) if 'suppressed_modal_candidates' in locals() else 'NA'}",
                file=sys.stderr, flush=True
            )
            # addons manager view 専用ロジック
            # 1) 必要な個別領域を組み立てる（regionsを直接いじらず dict を返すのが安全）
            modal_candidates = list(suppressed_modal_candidates) + list(modal_nodes_for_output)

            # ★ここが重要：regions 側に“材料”として戻す
            # regions["CONTENT"] が addons 本体を担うなら、そこへ合流（適切なキーは実装に合わせて）
            regions_for_am = dict(regions)
            regions_for_am["CONTENT"] = list(regions.get("CONTENT", [])) + modal_candidates

            am = self._build_addons_manager_view(regions_for_am, modal_candidates, screen_w, screen_h)

            print(
                f"[DEBUG-AM] built am sizes: "
                f"TABS={len(am.get('TABS', []))} "
                f"SIDENAV={len(am.get('SIDENAV', []))} "
                f"ADDONS_TOOLBAR={len(am.get('ADDONS_TOOLBAR', []))} "
                f"CONTENT={len(am.get('CONTENT', []))}",
                file=sys.stderr, flush=True
            )


            # 2) タブ周り（あれば）
            if r := self._compress_addons_tabs(am.get("TABS", [])):
                lines.append("=== TABS ===")
                lines.extend(r)
                lines.append("")

            # 3) 左ナビ（Recommendations/Extensions/Themes/Languages）
            if r := self._compress_addons_sidebar(am.get("SIDENAV", [])):
                lines.append("=== ADDONS SIDENAV ===")
                lines.extend(r)
                lines.append("")

            # 4) ツールバー（検索欄 etc）
            if r := self._compress_addons_toolbar(am.get("ADDONS_TOOLBAR", [])):
                lines.append("=== ADDONS TOOLBAR ===")
                lines.extend(r)
                lines.append("")

            # 5) メインコンテンツ（テーマ一覧など）
            if r := self._compress_addons_content(am.get("CONTENT", [])):
                lines.append("=== ADDONS CONTENT ===")
                lines.extend(r)
                lines.append("")



        elif view_type == "mail":
            # ----------------------------------------
            # Mail view 専用ロジック：
            # - MESSAGE_LIST（左ペイン）が MODAL に落ちた場合の救済（★追加）
            # - 右ペイン（閲覧ペイン）の本文が MODAL に落ちた場合の救済（既存）
            # - 3-pane の split_x / header_cut_y を動的推定（★反映）
            # ----------------------------------------

            MSG_LIST_LEFT_X = getattr(self, "MSG_LIST_LEFT_X", 360)

            candidates = (
                regions.get("MESSAGE_LIST", [])
                + regions.get("PREVIEW", [])
                + regions.get("MAIL_TOOLBAR", [])
            )

            SPLIT_MSG_LIST_X = self._estimate_split_msg_list_x(candidates, fallback=1040)
            HEADER_CUT_Y = self._estimate_msg_list_header_cut_y(
                candidates, MSG_LIST_LEFT_X, SPLIT_MSG_LIST_X, fallback=140
            )
            print(f"[DEBUG-ML] SPLIT_MSG_LIST_X={SPLIT_MSG_LIST_X} HEADER_CUT_Y={HEADER_CUT_Y}")

            # (1) 左ペイン救済（★id=11/12対策）
            modal_nodes_for_output, rescued_msg_list = self._rescue_message_list_from_modal(
                regions=regions,
                modal_nodes_for_output=modal_nodes_for_output,
                msg_left_x=MSG_LIST_LEFT_X,
                split_x=SPLIT_MSG_LIST_X,
                header_cut_y=HEADER_CUT_Y,
            )

            if rescued_msg_list:
                regions.setdefault("MESSAGE_LIST", [])
                existing = {id(n) for n in regions["MESSAGE_LIST"]}
                for n in rescued_msg_list:
                    if id(n) not in existing:
                        regions["MESSAGE_LIST"].append(n)
                print(f"[DEBUG] rescued MESSAGE_LIST nodes: {len(rescued_msg_list)}")

            # (2) mail view の false modal を減らす（既存）
            modal_nodes_for_output = self._reclassify_false_modals_in_mail(regions, modal_nodes_for_output)

            # (3) 右ペイン本文救済（既存）
            mail_area_nodes: List[Node] = []

            def add_mail_area_candidates(nodes: List[Node]) -> None:
                for n in nodes:
                    bbox = node_bbox_from_raw(n)
                    x, y = bbox["x"], bbox["y"]
                    tag = (n.get("tag") or "").lower()

                    if x >= SPLIT_MSG_LIST_X and tag in {
                        "document-web",
                        "section",
                        "label",
                        "link",
                        "image",
                        "paragraph",
                        "static",
                    }:
                        mail_area_nodes.append(n)

            add_mail_area_candidates(regions.get("PREVIEW", []))
            add_mail_area_candidates(regions.get("MESSAGE_LIST", []))
            add_mail_area_candidates(regions.get("MAIL_TOOLBAR", []))
            add_mail_area_candidates(regions.get("MODAL", []))
            add_mail_area_candidates(modal_nodes_for_output)

            mail_diff_nodes: List[Node] = []
            seen_ids = set()

            new_modal_region: List[Node] = []
            for n in regions.get("MODAL", []):
                if mail_area_nodes and self._is_inside_mail_area(n, mail_area_nodes):
                    nid = id(n)
                    if nid not in seen_ids:
                        seen_ids.add(nid)
                        mail_diff_nodes.append(n)
                else:
                    new_modal_region.append(n)
            regions["MODAL"] = new_modal_region

            new_modal_nodes_for_output: List[Node] = []
            for n in modal_nodes_for_output:
                if mail_area_nodes and self._is_inside_mail_area(n, mail_area_nodes):
                    nid = id(n)
                    if nid not in seen_ids:
                        seen_ids.add(nid)
                        mail_diff_nodes.append(n)
                else:
                    new_modal_nodes_for_output.append(n)
            modal_nodes_for_output = new_modal_nodes_for_output

            if mail_diff_nodes:
                regions.setdefault("PREVIEW", [])
                existing_ids = {id(n) for n in regions["PREVIEW"]}
                for n in mail_diff_nodes:
                    if id(n) not in existing_ids:
                        regions["PREVIEW"].append(n)

            mail_lines = self._build_mail_view(regions)
            lines.extend(mail_lines)


        elif view_type == "compose":
            cv = self._build_compose_view(regions, modal_nodes, screen_w, screen_h)

            lines.append("=== COMPOSE MENUBAR ===")
            lines.extend(self._compress_menubar(cv.get("MENUBAR", [])))

            lines.append("\n=== COMPOSE ACTIONS ===")
            lines.extend(self._compress_compose_actions(cv.get("ACTIONS", [])))

            lines.append("\n=== COMPOSE FIELDS ===")
            lines.extend(self._compress_compose_fields(cv.get("FIELDS", [])))

            lines.append("\n=== COMPOSE FORMATTING ===")
            lines.extend(self._compress_compose_formatting(cv.get("FORMATTING", [])))

            lines.append("\n=== COMPOSE BODY ===")
            lines.extend(self._compress_compose_body(cv.get("BODY", [])))


        else:
            # Generic / その他ビュー (古いメール画面など)
            if r := self._compress_folder_tree(
                regions["FOLDER_TREE"] + regions["SIDEBAR_HEADER"] + regions["SIDEBAR"]
            ):
                lines.append("=== FOLDERS ===")
                lines.extend(r)
                lines.append("")

            # ★ここ修正：MES　SAGE_LIST (全角スペース) はバグ
            if r_msg := self._compress_message_list(regions.get("MESSAGE_LIST", [])):
                lines.append("=== MESSAGE LIST ===")
                lines.extend(r_msg)
                lines.append("")

            if r_prev := self._compress_preview(regions["PREVIEW"]):
                lines.append("=== PREVIEW ===")
                lines.extend(r_prev)
                lines.append("")

            if regions["HOME_DASHBOARD"]:
                if r := self._compress_home_dashboard(regions["HOME_DASHBOARD"]):
                    lines.append("=== DASHBOARD CONTENT ===")
                    lines.extend(r)
                    lines.append("")

        # ----------------------------------------
        # 3) STATUSBAR (共通) — mail ビューだけは _build_mail_view 側で出力済み
        # ----------------------------------------
        if view_type != "mail":
            if r := self._compress_statusbar(regions["STATUSBAR"]):
                lines.append("=== STATUS BAR ===")
                lines.extend(r)
                lines.append("")

        # ----------------------------------------
        # 4) モーダル (MODAL / diff-modal の合体)
        # ----------------------------------------
        all_modals: List[Node] = []

        if regions.get("MODAL"):
            all_modals.extend(regions["MODAL"])

        if modal_nodes_for_output:
            all_modals.extend(modal_nodes_for_output)

        if all_modals:
            if r := self._compress_modal(all_modals):
                lines.append("=== MODAL / DIALOG ===")
                lines.extend(r)

        return lines
