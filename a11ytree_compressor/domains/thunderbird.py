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
        # ついでに Import セクションのボタンも短くしたければ
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
        
        # 1. OS & Spaces
        LAUNCHER_X_LIMIT = w * 0.05
        TOP_BAR_MAX_Y    = 50  # GNOME Bar
        
        # Spaces Bar: x=77, w=30 付近にある。右端は110pxあれば十分
        SPACES_BAR_MAX_X = 115 
        
        # 2. Vertical Splits (左右分割)
        # Spaces(115) < Sidebar < Split(400) < Main Content
        SPLIT_SIDEBAR_X = 400 
        SPLIT_LIST_X    = w * 0.55

        # 3. Horizontal Splits (上下分割)
        # Global Toolbar (Search等) は y=71付近
        # Sidebar Header (New Message等) は y=118付近
        # フォルダツリー開始は y=158付近
        TB_TOOLBAR_BOTTOM_Y = 100 
        SIDEBAR_HEADER_BOTTOM_Y = 150

        TB_STATUS_MIN_Y = h * 0.94

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
            
            if cy > TB_STATUS_MIN_Y:
                regions["STATUSBAR"].append(n)
                continue

            # --- 3. Thunderbird Left Columns (Spaces & Sidebar) ---
            
            # SPACES BAR (最左端)
            # 幅が狭いボタン(w<50)のみを対象にして、大きなパネルの誤判定を防ぐ
            if cx < SPACES_BAR_MAX_X and bw < 60:
                regions["SPACES_BAR"].append(n)
                continue

            # SIDEBAR AREA (Spacesより右、Splitより左)
            if SPACES_BAR_MAX_X <= cx < SPLIT_SIDEBAR_X:
                # 高さで ヘッダー(ボタン) と ツリー(中身) を分ける
                if cy < SIDEBAR_HEADER_BOTTOM_Y:
                    regions["SIDEBAR_HEADER"].append(n)
                else:
                    regions["SIDEBAR"].append(n)
                continue

            # SIDEBAR AREA (Spacesより右、Splitより左)
            if SPACES_BAR_MAX_X <= cx < SPLIT_SIDEBAR_X:
                # 高さで ヘッダー(ボタン) と ツリー(中身) を分ける
                if cy < SIDEBAR_HEADER_BOTTOM_Y:
                    regions["SIDEBAR_HEADER"].append(n)
                else:
                    # ★修正: SIDEBAR ではなく FOLDER_TREE に入れる
                    regions["FOLDER_TREE"].append(n)
                continue

            # --- 4. Main Content Area (残り右側) ---
            
            # Global Toolbar (上部の検索バーなど)
            if cy < TB_TOOLBAR_BOTTOM_Y:
                regions["TOOLBAR"].append(n)
                continue

            # Dashboard判定 (Home画面)
            if any(k in name_lower for k in self.DASHBOARD_KEYWORDS) or \
               (name_lower in {"address book", "account settings", "settings"}):
                # ★修正: DASHBOARD ではなく HOME_DASHBOARD に入れる
                regions["HOME_DASHBOARD"].append(n)
                continue
            
            # まだ分類されていない右側の要素を座標で分割
            if cx < SPLIT_LIST_X:
                # Dashboard表示中と思われる大きな見出し等はDashboardへ
                # ★修正: 判定基準を HOME_DASHBOARD に合わせる
                if regions["HOME_DASHBOARD"] and tag in {"heading", "paragraph", "label"} and bh > 20:
                     regions["HOME_DASHBOARD"].append(n)
                else:
                     regions["MESSAGE_LIST"].append(n)
            else:
                # ★修正: 同上
                if regions["HOME_DASHBOARD"] and tag in {"heading", "paragraph", "label", "link"}:
                     regions["HOME_DASHBOARD"].append(n)
                else:
                     regions["PREVIEW"].append(n)

        return regions

    # === フォーマット用ヘルパー (新規追加) ===
    def _format_node(self, n: Node) -> str:
        """標準的な [tag] "name" @ (cx, cy) 形式で出力"""
        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or n.get("text") or "").strip()
        
        # 名前がない場合はスキップするが、入力欄などは空でも出す場合がある
        # ここでは名前必須とする
        if not name:
            return ""
            
        return f"[{tag}] \"{name}\" @ ({cx}, {cy})"

    # === 圧縮関数群 (全面的に書き換え) ===

    def _compress_app_launcher(self, nodes: List[Node]) -> List[str]:
        lines = []
        # y座標順
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
        # x座標順
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
        # y座標順
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
        # x座標順
        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[0]
        )
        seen = set()
        for n in sorted_nodes:
            # 最小化などのOSボタンは除外
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

        # tree-item だけを抜き出して (y, x) でソート
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

        # ルート判定:
        # - メールアカウント: name に "@" を含む
        # - ローカルフォルダ: name == "Local Folders"
        def is_root_name(name: str) -> bool:
            if not name:
                return False
            lower = name.lower()
            if "@" in name:
                return True
            if lower == "local folders":
                return True
            return False

        # groups: [(root_node or None, [child_nodes...]), ...]
        groups: List[Tuple[Optional[Node], List[Node]]] = []
        current_root: Optional[Node] = None

        for n in items:
            name = (n.get("name") or "").strip()
            if not name:
                continue

            if is_root_name(name):
                # 新しいルート開始
                current_root = n
                groups.append((n, []))
            else:
                if current_root is None:
                    # ルートがまだ無い状態で現れたノード → 単独グループ扱い
                    groups.append((None, [n]))
                else:
                    # 直近のルートの子として追加
                    groups[-1][1].append(n)

        # 出力に整形
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
                    # 子要素はインデントして階層を示す
                    lines.append("  " + child_line)
            else:
                # ルートを持たない孤立ノード（ほぼ無いはずだが念のため）
                for c in children:
                    child_line = self._format_node(c)
                    if not child_line or child_line in seen_keys:
                        continue
                    seen_keys.add(child_line)
                    lines.append(child_line)

        return lines


    # === Home Dashboard のセクション分割ロジック ===
    def _split_home_sections(self, nodes: List[Node]) -> Dict[str, List[Node]]:
        """
        Home画面を "Set Up Another Account", "Import from Another Program" 等の
        セクションごとに分割する。
        """
        sections: Dict[str, List[Node]] = {}
        
        # Y座標でソート
        nodes = sorted(nodes, key=lambda n: node_bbox_from_raw(n)["y"])
        
        current_section = "Unknown"
        sections[current_section] = []
        
        # セクションヘッダーになりうるキーワード
        section_headers = {
            "Set Up Another Account",
            "Import from Another Program",
            "About Mozilla Thunderbird",
            "Resources"
        }

        for n in nodes:
            name = (n.get("name") or "").strip()
            # 見出しっぽい要素があればセクション切り替え
            if name in section_headers:
                current_section = name
                if current_section not in sections:
                    sections[current_section] = []
                # ヘッダー自体もそのセクションに含める
                sections[current_section].append(n)
            else:
                sections[current_section].append(n)
                
        return sections

    def _compress_home_dashboard(self, nodes: List[Node]) -> List[str]:
        """
        中央ダッシュボード: 
        セクション分割ロジックは維持しつつ、中身は要約せずそのまま出力する。
        """
        if not nodes: return []

        # セクションごとにグループ化 (順序維持のため)
        sections = self._split_home_sections(nodes)
        
        # セクションの出現順序を決定 (Y座標ベース)
        # _split_home_sections が返す辞書のキー順序は保証されないため、
        # 各セクションの最初のノードのY座標でソートする
        sorted_sections = []
        for title, section_nodes in sections.items():
            if section_nodes:
                min_y = min(node_bbox_from_raw(n)["y"] for n in section_nodes)
                sorted_sections.append((min_y, title, section_nodes))
        sorted_sections.sort(key=lambda x: x[0])

        lines: List[str] = []
        seen_keys = set()

        # ヘッダーに属さない上部のボタン群 (Account Actionsなど) を先に回収
        # _split_home_sections に漏れたものがあればここで出す
        all_section_node_ids = {id(n) for _, _, sn in sorted_sections for n in sn}
        orphans = [n for n in nodes if id(n) not in all_section_node_ids]
        
        if orphans:
            orphans.sort(key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
            for n in orphans:
                l = self._format_node(n)
                if l and l not in seen_keys:
                    seen_keys.add(l)
                    lines.append(l)
            if lines: lines.append("") # 空行

        # 各セクションの出力
                # 各セクションの出力
        for _, title, section_nodes in sorted_sections:
            # セクション内のノードを (Y, X) でソート
            section_nodes.sort(key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
            
            for n in section_nodes:
                node_for_print = n  # デフォルトはそのまま
                tag = (n.get("tag") or "").lower()
                name = (n.get("name") or "").strip()

                # ★ 「Set Up Another Account」内の push-button だけ短いラベルに変換
                if title == "Set Up Another Account" and tag == "push-button":
                    short = self.ACCOUNT_SETUP_BUTTON_SHORT.get(name)
                    if short:
                        # 元ノードは壊したくないのでコピーして name だけ差し替える
                        node_copy = dict(n)
                        node_copy["name"] = short
                        node_for_print = node_copy

                # あとは共通フォーマッタに渡す
                l = self._format_node(node_for_print)
                if not l or l in seen_keys:
                    continue
                seen_keys.add(l)
                lines.append(l)

            lines.append("")  # セクション区切り


        return lines

    def _compress_message_list(self, nodes: List[Node]) -> List[str]:
        lines = []
        # y座標順
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
        # x座標順
        nodes.sort(key=lambda n: node_bbox_from_raw(n)["x"])
        for n in nodes:
            line = self._format_node(n)
            if line: lines.append(line)
        return lines

    def _compress_modal(self, nodes: List[Node]) -> List[str]:
        lines = []
        # y, x順
        nodes.sort(key=lambda n: (node_bbox_from_raw(n)["y"], node_bbox_from_raw(n)["x"]))
        for n in nodes:
            line = self._format_node(n)
            if line: lines.append(line)
        return lines
    


    def _detect_view_type(self, regions: Dict[str, List[Node]]) -> str:
        """
        ざっくり画面種別を判定する:
          - "home": Thunderbird起動画面のホームダッシュボード
          - "other": それ以外
        まずは id=1,2 のホーム画面だけを確実に見分ける用途に限定する。
        """
        def names(nodes: List[Node]) -> Set[str]:
            s: Set[str] = set()
            for n in nodes:
                name = (n.get("name") or "").strip()
                if name:
                    s.add(name.lower())
            return s

        # Home 用に集めている領域（あなたの get_semantic_regions の設計に合わせる）
        home_nodes: List[Node] = []
        home_nodes.extend(regions.get("HOME_DASHBOARD", []))
        home_nodes.extend(regions.get("DASHBOARD", []))

        if not home_nodes:
            return "other"

        home_names = names(home_nodes)

        # ホーム画面特有の見出し
        HOME_HEADERS = {
            "set up another account",
            "import from another program",
            "about mozilla thunderbird",
            "resources",
        }

        if HOME_HEADERS & home_names:
            return "home"

        return "other"






    def _build_output(
        self,
        regions: Dict[str, List[Node]],
        modal_nodes: List[Node],  # BaseA11yCompressor 側の diff モーダル検出結果
        screen_w: int,
        screen_h: int,
    ) -> List[str]:
        """
        領域ごとの出力順序を定義する。
        既存の各 _compress_xxx の処理は変えずに、
        「どの画面種別でどれを呼ぶか」だけここで制御する。
        """
        lines: List[str] = []

        # 0. 画面種別の判定 (まずは Home だけを特別扱い)
        view_type = self._detect_view_type(regions)

        # 1. SYSTEM / LAUNCHER / TOOLBAR / SPACES
        if r := self._compress_top_bar(regions["TOP_BAR"]):
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

        # 2. 左側フォルダツリー（既存の folder_tree 圧縮ロジックをそのまま利用）
        if r := self._compress_folder_tree(
            regions["FOLDER_TREE"]
            + regions["SIDEBAR_HEADER"]
            + regions["SIDEBAR"]
        ):
            lines.append("=== FOLDERS ===")
            lines.extend(r)
            lines.append("")

        # 3. メインコンテンツ
        if view_type == "home":
            # Home 画面の場合: 既存ロジック通り HOME_DASHBOARD を優先的に出す
            if r := self._compress_home_dashboard(
                regions["HOME_DASHBOARD"] + regions["DASHBOARD"]
            ):
                lines.append("=== HOME DASHBOARD ===")
                lines.extend(r)
                lines.append("")
        else:
            # それ以外の画面: メッセージリスト＋プレビューを出す
            # （元の「r が空なら MESSAGE_LIST / PREVIEW」というフォールバック部分をこちらに移動）
            if r_msg := self._compress_message_list(regions["MESSAGE_LIST"]):
                lines.append("=== MESSAGE LIST ===")
                lines.extend(r_msg)
                lines.append("")
            if r_prev := self._compress_preview(regions["PREVIEW"]):
                lines.append("=== PREVIEW ===")
                lines.extend(r_prev)
                lines.append("")

        # 4. 下部ステータスバー
        if r := self._compress_statusbar(regions["STATUSBAR"]):
            lines.append("=== STATUS BAR ===")
            lines.extend(r)
            lines.append("")
        
        # 5. モーダル (現状の挙動を維持)
        all_modals = regions["MODAL"] + (modal_nodes or [])
        if r := self._compress_modal(all_modals):
            lines.append("=== MODAL / DIALOG ===")
            lines.extend(r)

        return lines
