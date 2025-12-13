import re
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


    def _detect_view_type(self, nodes: List[Node]) -> str:
        def tag(n): return (n.get("tag") or "").lower()
        def name(n): return (n.get("name") or "").strip()
        def lower_name(n): return name(n).lower()

        # ----------------------------
        # 1) HARD GUARDS (確定ルール)
        # ----------------------------
        # Add-ons Manager
        if any(tag(n) == "document-web" and "add-ons manager" in lower_name(n) for n in nodes):
            return "addons_manager"

        # Account Settings
        if any("account settings -" in lower_name(n) for n in nodes):
            return "account_settings"

        # ----------------------------
        # 2) SCORE-BASED (柔らかい判定)
        # ----------------------------
        score: Dict[str, float] = defaultdict(float)

        # --- mail signals ---
        mail_keywords = {"quick filter", "message list display options"}
        if any(lower_name(n) in mail_keywords for n in nodes):
            score["mail"] += 3

        # tree-item にカンマ含む行が複数ある → メール一覧っぽい
        msg_row_hits = sum(1 for n in nodes if tag(n) == "tree-item" and "," in name(n))
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
        home_heading_hits = sum(1 for n in nodes if tag(n) == "heading" and lower_name(n) in home_headings)
        score["home"] += min(home_heading_hits * 2, 6)

        # --- settings signals ---
        if any(tag(n) == "document-web" and lower_name(n) == "settings" for n in nodes):
            score["settings"] += 6  # hard guardにしなかった場合の強シグナル
        settings_nav = {"general", "composition", "privacy & security", "chat"}
        nav_hits = sum(1 for n in nodes if tag(n) in {"list-item", "label"} and lower_name(n) in settings_nav)
        score["settings"] += min(nav_hits, 4)

        if any(tag(n) == "section" and name(n) == "Settings" for n in nodes):
            score["settings"] += 2

        # --- addons signals（hard guard落ちた時の保険） ---
        if any(tag(n) == "section" and lower_name(n) == "add-ons manager" for n in nodes):
            score["addons_manager"] += 4
        addons_nav = {"recommendations", "extensions", "themes", "languages"}
        addons_hits = sum(1 for n in nodes if lower_name(n) in addons_nav)
        score["addons_manager"] += min(addons_hits, 4)

        # ----------------------------
        # 3) 決定ロジック
        # ----------------------------
        # 最小閾値：これ未満なら unknown（誤判定防止）
        MIN_SCORE = 3.0

        best_view, best_score = max(score.items(), key=lambda kv: kv[1], default=("unknown", 0.0))

        # 同点があるなら unknown（危険回避）
        top = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
        if not top or top[0][1] < MIN_SCORE:
            return "unknown"
        if len(top) >= 2 and abs(top[0][1] - top[1][1]) < 0.5:
            return "unknown"

        return best_view

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

        # クールダウン中は modal を “出力対象から除外”
        if cooldown > 0:
            regions["MODAL"] = []
            modal_nodes = []
            lines.append(f"DEBUG_MODAL_SUPPRESSED: cooldown={cooldown}, prev={prev_view}, curr={view_type}")
            print(f"[DEBUG] MODAL_SUPPRESSED cooldown={cooldown} prev={prev_view} curr={view_type}")

        setattr(self, "_prev_view_type", view_type)
        setattr(self, "_view_change_cooldown", max(0, cooldown - 1))

        # diff 由来のモーダル候補はここで管理
        modal_nodes_for_output: List[Node] = list(modal_nodes or [])

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
