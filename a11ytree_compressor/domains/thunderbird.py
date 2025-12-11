import re
from typing import List, Dict, Tuple, Set, Optional, Any
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
        
        # ★修正: ステータスバーの開始位置を厳密にする
        # Add-ons(1001), Colors(1031) より下で、Hide Spaces(1058) と同等か下
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
            
            # --- 3. Thunderbird Left Columns (優先度アップ) ---
            # ★修正: ステータスバー判定より先にSPACESを判定する
            # これにより "Settings"(y=1013) や "Hide Spaces"(y=1058) がステータスバーに行かなくなる
            if cx < SPACES_BAR_MAX_X and bw < 60:
                regions["SPACES_BAR"].append(n)
                continue

            if SPACES_BAR_MAX_X <= cx < SPLIT_SIDEBAR_X:
                if cy < SIDEBAR_HEADER_BOTTOM_Y:
                    regions["SIDEBAR_HEADER"].append(n)
                else:
                    regions["FOLDER_TREE"].append(n)
                continue

            # --- 4. Status Bar (条件厳格化) ---
            is_statusbar_item = False
            
            # 名前指定: 画面上部にあるボタン等との誤爆を防ぐため y > 500
            if name in {"Done", "You are currently online.", "Home"}:
                if cy > 500: 
                    is_statusbar_item = True
            
            # 座標指定: 
            # 1. BOTTOM_AREA_Y (1060) より下にある
            # 2. かつ、画面の物理的な下端 (1080) より上にあること
            #    これがないと、スクロール先の要素 (y=2691) がステータスバー扱いになって消滅する
            elif cy > BOTTOM_AREA_Y and cy < 1080:
                is_statusbar_item = True
            
            if is_statusbar_item:
                regions["STATUSBAR"].append(n)
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


    def _detect_view_type(self, nodes: List[Node]) -> str:
        """
        Decide which Thunderbird view this is:
        - 'home'            : top dashboard (Set Up Another Account ... )
        - 'settings'        : Thunderbird Settings (General / Composition / ...)
        - 'account_settings': Account Settings for a specific account
        - 'generic'         : fallback
        """
        names = { (n.get("name") or "").strip() for n in nodes }
        lower_names = { n.lower() for n in names if n }

        # 1) Home dashboard
        if "set up another account" in lower_names or "import from another program" in lower_names:
            return "home"

        # 2) Account Settings view
        #   - title "Account Settings"
        #   - or label "Account Settings - <account>"
        if any("account settings -" in ln for ln in lower_names) or "account settings" in lower_names:
            # ただし Settings 画面の左下リンク "Account Settings" だけで誤検出しないように、
            # tree-item や entry の "Account Name:" 等があるかも併せて確認する
            has_account_name_label = any(
                (n.get("name") or "").strip() == "Account Name:"
                for n in nodes
            )
            if has_account_name_label:
                return "account_settings"

        # 3) Thunderbird Settings view (General / Composition / Privacy & Security ...)
        # 左サイドに list-item "General", "Composition", etc. が並び、
        # 中央上に heading "General" などが出るパターンを検出
        settings_side_candidates = {"General", "Composition", "Privacy & Security", "Chat"}
        has_settings_section = any(
            (n.get("tag") or "").lower() == "section"
            and (n.get("name") or "").strip() == "Settings"
            for n in nodes
        )
        has_settings_nav = any(
            (n.get("tag") or "").lower() in {"list-item", "label"}
            and (n.get("name") or "").strip() in settings_side_candidates
            for n in nodes
        )
        # 条件を少し緩めて、SettingsセクションがなくてもNavがあればSettingsとみなす(誤検出あれば調整)
        if has_settings_section or has_settings_nav:
            return "settings"

        # 4) fallback
        return "generic"


    def _build_output(
        self,
        regions: Dict[str, List[Node]],
        modal_nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> List[str]:
        """
        領域ごとの出力順序を定義する。
        """
        lines: List[str] = []

        all_nodes = []
        for lst in regions.values():
            all_nodes.extend(lst)
        view_type = self._detect_view_type(all_nodes)

        # --- 共通部分 ---
        
        # ★修正: SYSTEM見出しを確実に出力
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

        # 3. メインコンテンツ
        if view_type == "home":
            if r := self._compress_folder_tree(
                regions["FOLDER_TREE"] + regions["SIDEBAR_HEADER"] + regions["SIDEBAR"]
            ):
                lines.append("=== FOLDERS ===")
                lines.extend(r)
                lines.append("")
            
            if r := self._compress_home_dashboard(
                regions["HOME_DASHBOARD"] + regions["DASHBOARD"]
            ):
                lines.append("=== HOME DASHBOARD ===")
                lines.extend(r)
                lines.append("")

        elif view_type == "settings" or view_type == "account_settings":
            settings_lines = self._compress_settings_view(regions, screen_w, screen_h)
            if settings_lines:
                lines.extend(settings_lines)
            else:
                if r := self._compress_home_dashboard(regions["HOME_DASHBOARD"]):
                    lines.append("=== SETTINGS (Fallback) ===")
                    lines.extend(r)

        else:
            if r := self._compress_folder_tree(
                regions["FOLDER_TREE"] + regions["SIDEBAR_HEADER"] + regions["SIDEBAR"]
            ):
                lines.append("=== FOLDERS ===")
                lines.extend(r)
                lines.append("")

            if r_msg := self._compress_message_list(regions["MESSAGE_LIST"]):
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

        # 4. 下部ステータスバー (共通)
        # ★ここが重要: 最後に必ずSTATUS BARを出力
        if r := self._compress_statusbar(regions["STATUSBAR"]):
            lines.append("=== STATUS BAR ===")
            lines.extend(r)
            lines.append("")
        
        # 5. モーダル
        all_modals = regions["MODAL"] + (modal_nodes or [])
        if r := self._compress_modal(all_modals):
            lines.append("=== MODAL / DIALOG ===")
            lines.extend(r)

        return lines