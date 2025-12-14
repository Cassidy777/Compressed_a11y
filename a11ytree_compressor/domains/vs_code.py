import re
from typing import List, Dict, Tuple, Set, Optional, Any

from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    dedup_horizontal_menu_nodes,
)

class Vs_codeCompressor(BaseA11yCompressor):
    domain_name = "vs_code"


    # ----------------------------
    # セマンティック領域分割 (VS Code)
    # ----------------------------
    def get_semantic_regions(
        self, nodes: List[Node], w: int, h: int, dry_run: bool = False
    ) -> Dict[str, List[Node]]:
        """
        VS Code: まずは「領域分け」だけを安定させる。
        MODALの救助は _build_output 側でやる（=ここでは極力 MODAL に入れない）。
        """
        regions: Dict[str, List[Node]] = {
            "APP_LAUNCHER": [],
            "MENUBAR": [],
            "ACTIVITY_BAR": [],
            "SIDE_BAR": [],
            "TAB_BAR": [],
            "BREADCRUMB": [],
            "FIND_REPLACE": [],
            "CONTENT": [],
            "STATUSBAR": [],
            "MODAL": [],  # 基本空でOK（diff-modalが来た場合はengine側で渡ってくる）
        }

        w = max(1, int(w))
        h = max(1, int(h))

        def _tag(n: Node) -> str:
            return (n.get("tag") or "").lower()

        def _name(n: Node) -> str:
            return (n.get("name") or n.get("text") or "").strip()

        # ---- しきい値（あなたの例の座標に合わせて、比率ベースで置く）----
        LAUNCHER_X_MAX = w * 0.05          # Ubuntu左端ドック（x=0の列）
        TOP_Y_MAX = h * 0.20               # 上部バー帯
        STATUS_Y_MIN = h * 0.96            # 下部ステータスバー帯

        MENUBAR_Y_MAX = h * 0.12           # メニューはかなり上
        TABBAR_Y_MIN = h * 0.07            # タブは上の方（y=89前後）
        TABBAR_Y_MAX = h * 0.16

        ACTIVITY_X_MIN = w * 0.02          # activity bar（x=70付近）
        ACTIVITY_X_MAX = w * 0.08

        SIDEBAR_X_MIN = w * 0.06           # 左ペイン（x=118〜360ぐらい）
        SIDEBAR_X_MAX = w * 0.22

        # breadcrumb（/home/user/...）が出る帯（y=124付近）
        BREAD_Y_MIN = h * 0.10
        BREAD_Y_MAX = h * 0.18

        # Find/Replace（上部右寄りの帯：例1では y=146 付近、x>1300）
        FR_Y_MIN = h * 0.12
        FR_Y_MAX = h * 0.28
        FR_X_MIN = w * 0.60

        MENU_KEYWORDS = {
            "file", "edit", "selection", "view", "go", "run", "terminal", "help"
        }

        # ---- 1st pass: 優先度順で分類 ----
        for n in nodes:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

            tag = _tag(n)
            name = _name(n)
            lname = name.lower()

            # 1) APP_LAUNCHER（Ubuntu dock）
            if x <= LAUNCHER_X_MAX and bw <= w * 0.08 and bh >= 30:
                if tag in ("push-button", "toggle-button", "launcher-app", "button"):
                    regions["APP_LAUNCHER"].append(n)
                    continue

            # 2) STATUSBAR（最下部）
            if cy >= STATUS_Y_MIN:
                regions["STATUSBAR"].append(n)
                continue

            # 3) MENUBAR（上部）
            if cy <= MENUBAR_Y_MAX:
                # a11yでは push-button / menu の両方があり得るので両対応
                if tag in ("menu", "push-button") and lname in MENU_KEYWORDS:
                    regions["MENUBAR"].append(n)
                    continue

            # 4) ACTIVITY_BAR（左の縦アイコン列：x=70,w=48）
            if ACTIVITY_X_MIN <= cx <= ACTIVITY_X_MAX and y <= h * 0.93:
                # section / push-button が混ざる想定
                if tag in ("section", "push-button", "toggle-button", "button", "static"):
                    regions["ACTIVITY_BAR"].append(n)
                    continue

            # 5) TAB_BAR（Welcome / ファイル名タブ + close）
            if TABBAR_Y_MIN <= cy <= TABBAR_Y_MAX:
                if tag in ("section", "push-button", "tab", "label", "static"):
                    # 「Close」「Welcome」「拡張子」「- Visual Studio Code」など
                    if (
                        "close (ctrl+w)" in lname
                        or "welcome" in lname
                        or "visual studio code" in lname
                        or any(ext in lname for ext in (".py", ".txt", ".md", ".json", ".yaml", ".yml"))
                    ):
                        regions["TAB_BAR"].append(n)
                        continue

            # 6) BREADCRUMB（/home/user/...）
            if BREAD_Y_MIN <= cy <= BREAD_Y_MAX:
                if ("/" in name) or (lname in {"home", "user", "desktop"}) or ("" in name):
                    # パンくずは section/static/label が混ざる
                    if tag in ("section", "static", "label", "text", "link"):
                        regions["BREADCRUMB"].append(n)
                        continue

            # 7) FIND_REPLACE（上部右寄り）
            if FR_Y_MIN <= cy <= FR_Y_MAX and cx >= FR_X_MIN:
                if tag in ("entry", "check-box", "push-button", "toggle-button", "label", "text", "section", "static"):
                    # Find/Replace 周辺は “Find”“Replace”“Match Case”などが多い
                    if any(k in lname for k in ("find", "replace", "match", "regex", "selection", "previous", "next", "toggle")):
                        regions["FIND_REPLACE"].append(n)
                        continue

            # 8) SIDE_BAR（Explorer/検索/アウトライン等の左ペイン）
            if SIDEBAR_X_MIN <= cx <= SIDEBAR_X_MAX and cy <= h * 0.93:
                if tag in ("section", "push-button", "toggle-button", "tree-item", "list-item", "heading", "label", "static", "text"):
                    regions["SIDE_BAR"].append(n)
                    continue

            # 9) 残りは CONTENT
            regions["CONTENT"].append(n)

        # 軽い後処理：メニューの横並び重複を削る（あれば）
        if regions["MENUBAR"]:
            regions["MENUBAR"] = dedup_horizontal_menu_nodes(regions["MENUBAR"])

        return regions


    # ----------------------------
    # 圧縮（最低限）：各リージョン
    # ----------------------------
    def _format_center(self, n: Node) -> str:
        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        return f"@ ({cx}, {cy})"


    def _compress_menubar(self, nodes: List[Node]) -> List[str]:
        # 横並び重複を落とす（共通関数流用）
        deduped = dedup_horizontal_menu_nodes(nodes)
        order = ["File","Edit","Selection","View","Go","Run","Terminal","Help"]

        # name を優先して、順序は上の order をできるだけ保つ
        bucket = {}
        for n in deduped:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            bucket.setdefault(name.lower(), n)

        lines = []
        for k in order:
            n = bucket.get(k.lower())
            if n:
                lines.append(f'[menu] "{k}" {self._format_center(n)}')
        # 取りこぼしも少しだけ追加
        for low, n in bucket.items():
            if low not in {k.lower() for k in order}:
                name = (n.get("name") or n.get("text") or "").strip()
                lines.append(f'[menu] "{name}" {self._format_center(n)}')
        return lines


    def _compress_simple_list(self, nodes: List[Node], allow_tags: Optional[Set[str]] = None, max_items: int = 25) -> List[str]:
        lines = []
        seen = set()
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if allow_tags and tag not in allow_tags:
                continue
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            key = (tag, name.lower())
            if key in seen:
                continue
            seen.add(key)
            lines.append(f'[{tag}] "{name}" {self._format_center(n)}')
            if len(lines) >= max_items:
                break
        return lines


    def _compress_statusbar(self, nodes: List[Node]) -> List[str]:
        # statusbar は情報量が多いので label/text を優先して短く
        allow = {"label", "text", "status", "statusbar", "push-button"}
        return self._compress_simple_list(nodes, allow_tags=allow, max_items=18)


    def _compress_editor(self, nodes: List[Node]) -> List[str]:
        # 最初は “見出し/リンク/ボタン/タブ/重要ラベル” だけ拾う（ノイズ避け）
        allow = {"heading", "label", "text", "link", "push-button", "toggle-button", "tab", "paragraph", "list-item", "tree-item", "entry"}
        return self._compress_simple_list(nodes, allow_tags=allow, max_items=30)

    
    def _compress_content_by_view(self, nodes: List[Node], view_type: str) -> List[str]:
        """
        CONTENT（中央）を view ごとに圧縮する。
        ご指摘の通り、section/static/label を広めに許可して取りこぼしを防ぐ。
        """
        view_type = view_type or "generic"

        if view_type == "welcome":
            allow = {"heading", "paragraph", "push-button", "link", "label", "text", "section", "static"}
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=28)

        if view_type == "settings":
            # Settings: 検索欄(entry) + 項目(tree/list) + チェックボックス
            # 構造化タグが欠落した場合に備え、section/static/label も許可
            allow = {
                "entry", "heading", "label", "text", "push-button", 
                "check-box", "combo-box", "list-item", "tree-item",
                "section", "static", "paragraph"
            }
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=40)
            
        if view_type == "extensions":
            # Extensions: Installボタン(push-button)や見出し
            allow = {
                "entry", "push-button", "heading", "label", "paragraph", "link",
                "section", "static"
            }
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=30)

        if view_type == "command_palette":
            # Palette: 入力欄 + リスト
            allow = {"entry", "list-item", "label", "text", "static"}
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=20)

        if view_type == "editor":
            # Editor: 本文の行が見えにくい場合があるので entry/label/paragraph を重視
            allow = {
                "entry", "label", "text", "heading", "paragraph", "push-button", "link",
                "document-web", "document-frame"
            }
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=30)

        # generic fallback
        # なんでも拾う
        allow = {
            "heading", "label", "text", "push-button", "link", "entry", "section", 
            "check-box", "combo-box", "static", "paragraph", "document-web"
        }
        return self._compress_simple_list(nodes, allow_tags=allow, max_items=30)


    # ----------------------------
    # view 判定
    # ----------------------------

    def _detect_view_type(self, nodes: List[Node]) -> str:
        """
        VS Code View Type Detector (Score-based).
        bbox依存を排除し、テキストとタグの強固なシグナルのみで判定する。
        """
        from collections import defaultdict

        def tag(n): 
            return (n.get("tag") or "").lower()

        def disp(n):
            return (n.get("name") or n.get("text") or n.get("description") or "").strip()

        def ldisp(n):
            return disp(n).lower()

        # --- fast text blob (for contains) ---
        # 全体の傾向を掴むため、有効なテキストを結合して検査用文字列を作る
        blob = " ".join(ldisp(n) for n in nodes if disp(n))

        # ----------------------------
        # 1) STRONG GUARDS (確定ルール)
        # ----------------------------

        # --- Command Palette ---
        # テキストによる強判定 (bbox計算不要でimportエラー回避)
        if ("type the name of a command" in blob
            or "show and run commands" in blob
            or blob.strip().startswith(">")):
            return "command_palette"

        # --- Settings ---
        # タブ/見出し/ページタイトルで Settings が強く出たら確定
        for n in nodes:
            t = tag(n)
            s = ldisp(n)
            # タブやヘッダに明確に "Settings" がある
            if t in {"heading", "tab", "label"} and s == "settings":
                return "settings"
            # 検索欄 (Search Settings) はかなり強い特徴
            if t == "entry" and "search settings" in s:
                return "settings"

        # --- Extensions ---
        # Extensions検索欄 or Marketplace が出たらほぼ確定
        for n in nodes:
            t = tag(n)
            s = ldisp(n)
            if t == "entry" and "search extensions" in s and "marketplace" in s:
                return "extensions"
            if "extensions: marketplace" in s:
                return "extensions"

        # ----------------------------
        # 2) SCORE-BASED (加点方式)
        # ----------------------------
        score: Dict[str, float] = defaultdict(float)

        for n in nodes:
            t = tag(n)
            s = ldisp(n)

            # --- Welcome signals ---
            if s in {"visual studio code", "get started", "walkthroughs", "recent", "start"}:
                score["welcome"] += 3.0 if t == "heading" else 1.0

            if s in {"new file", "open file...", "open folder...", "clone git repository..."}:
                if t in {"push-button", "link", "label"}:
                    score["welcome"] += 2.0

            # --- Editor signals ---
            # Statusbar 情報 (Ln, Col, Encode, EOL)
            if s.startswith("ln ") or " col " in s: # "Ln 1, Col 1" 対応
                score["editor"] += 2.5
            if s in {"utf-8", "lf", "crlf", "plain text"} or s.startswith("spaces:") or s.startswith("tab size:"):
                score["editor"] += 2.0
            # ★追加: スクリーンリーダーモード未有効時の警告メッセージはエディタ確定級の証拠
            if "editor is not accessible" in s or "enable screen reader optimized mode" in s:
                score["editor"] += 5.0

            # document-web / title suffix (ウィンドウタイトルなど)
            # document-frame も考慮
            if t in {"document-web", "document-frame"} and "visual studio code" in s:
                score["editor"] += 4.0
            if " - visual studio code" in s:
                score["editor"] += 3.0

            # --- Settings signals ---
            if s in {"user settings", "workspace settings", "text editor", "workbench", "window", "features", "application"}:
                score["settings"] += 1.5
            if s in {"font size", "font family", "tab size", "cursor style"}:
                score["settings"] += 1.5
            if t == "check-box" and ("settings" in blob or "search settings" in blob):
                score["settings"] += 0.5

            # --- Extensions signals ---
            if s in {"installed", "recommended", "enabled", "disabled"}:
                score["extensions"] += 1.5
            if s in {"install", "uninstall", "reload required"} and t in {"push-button", "button"}:
                score["extensions"] += 2.5
            if "publisher" in s or "downloads" in s:
                score["extensions"] += 0.8

        # ----------------------------
        # 3) DECISION (決定)
        # ----------------------------
        top = sorted(score.items(), key=lambda kv: kv[1], reverse=True)

        # 何も根拠がないなら generic (Editorと決めつけない)
        if not top:
            return "generic"

        best_view, best_score = top[0]

        # 最低限の確度 (MIN_SCORE)
        MIN_SCORE = 3.0
        if best_score < MIN_SCORE:
            # 決定打に欠ける場合、「Editorっぽい強い証拠」が一つでもあれば Editor に倒す
            # 例: ウィンドウタイトルやdocument-webの存在
            is_editor_strong_evidence = (" - visual studio code" in blob) or any(
                (tag(n) in {"document-web", "document-frame"} and "visual studio code" in ldisp(n))
                for n in nodes
            )
            
            if is_editor_strong_evidence:
                return "editor"
            
            # それ以外は安全に generic
            return "generic"

        return best_view



    # ----------------------------
    # Modal 関連
    # ----------------------------
    def _rescue_from_modal(
        self,
        merged_modal: List[Node],
        regions: Dict[str, List[Node]],
        w: int,
        h: int,
    ) -> Tuple[List[Node], List[Node]]:
        """
        diff-modal が誤って吸った要素を、幾何＋軽い文字判定で救助して適切な領域へ戻す。
        戻せないものだけ MODAL/NOTIFICATION に残す。
        """
        rescued: List[Node] = []
        remain: List[Node] = []

        w = max(1, int(w))
        h = max(1, int(h))

        def _tag(n: Node) -> str:
            return (n.get("tag") or "").lower()

        def _name(n: Node) -> str:
            return (n.get("name") or n.get("text") or "").strip()

        STATUS_Y_MIN = h * 0.96
        MENUBAR_Y_MAX = h * 0.12
        TAB_Y_MIN, TAB_Y_MAX = h * 0.07, h * 0.16
        ACT_X_MIN, ACT_X_MAX = w * 0.02, w * 0.08
        FR_Y_MIN, FR_Y_MAX, FR_X_MIN = h * 0.12, h * 0.28, w * 0.60
        BREAD_Y_MIN, BREAD_Y_MAX = h * 0.10, h * 0.18

        MENU_KEYWORDS = {"file", "edit", "selection", "view", "go", "run", "terminal", "help"}

        for n in merged_modal:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

            tag = _tag(n)
            name = _name(n)
            lname = name.lower()

            # 1) STATUSBAR救助
            if cy >= STATUS_Y_MIN:
                regions["STATUSBAR"].append(n)
                rescued.append(n)
                continue

            # 2) MENUBAR救助
            if cy <= MENUBAR_Y_MAX and tag in ("menu", "push-button") and lname in MENU_KEYWORDS:
                regions["MENUBAR"].append(n)
                rescued.append(n)
                continue

            # 3) ACTIVITY_BAR救助
            if ACT_X_MIN <= cx <= ACT_X_MAX and tag in ("section", "push-button", "toggle-button", "static"):
                regions["ACTIVITY_BAR"].append(n)
                rescued.append(n)
                continue

            # 4) TAB_BAR救助
            if TAB_Y_MIN <= cy <= TAB_Y_MAX and (
                "close (ctrl+w)" in lname
                or "welcome" in lname
                or "visual studio code" in lname
                or any(ext in lname for ext in (".py", ".txt", ".md", ".json", ".yaml", ".yml"))
            ):
                regions["TAB_BAR"].append(n)
                rescued.append(n)
                continue

            # 5) BREADCRUMB救助
            if BREAD_Y_MIN <= cy <= BREAD_Y_MAX and ("/" in name or lname in {"home", "user", "desktop"} or "" in name):
                regions["BREADCRUMB"].append(n)
                rescued.append(n)
                continue

            # 6) FIND_REPLACE救助（これが一番重要）
            if FR_Y_MIN <= cy <= FR_Y_MAX and cx >= FR_X_MIN:
                if tag in ("entry", "check-box", "push-button", "label", "text", "section", "static"):
                    if any(k in lname for k in ("find", "replace", "match", "regex", "previous", "next", "toggle", "selection")):
                        regions["FIND_REPLACE"].append(n)
                        rescued.append(n)
                        continue

            # 戻せないものだけ modal として残す
            remain.append(n)

        return rescued, remain


    # ----------------------------
    # メイン出力
    # ----------------------------
    def _build_output(
        self,
        regions: Dict[str, List[Node]],
        modal_nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> List[str]:
        lines: List[str] = []
        w, h = max(1, int(screen_w)), max(1, int(screen_h))

        # 1) merged modal（regions由来 + diff-modal由来）
        merged_modal = list(regions.get("MODAL") or [])
        if modal_nodes:
            merged_modal.extend(list(modal_nodes))

        # 2) MODAL救助（出力直前に戻す）
        #    Find/Replaceなどを元の場所に戻す
        if merged_modal:
            _rescued, remaining_modal = self._rescue_from_modal(merged_modal, regions, w, h)
        else:
            remaining_modal = []

        # 3) view判定 (★修正したメソッドを使用)
        #    CONTENTだけでなく、MODALも含めて判定しないとパレットを見逃す可能性があるため結合して渡す
        nodes_for_detection = (
            (regions.get("CONTENT") or []) +
            (regions.get("TAB_BAR") or []) +
            (regions.get("STATUSBAR") or []) +
            (regions.get("BREADCRUMB") or []) +
            remaining_modal
        )
        view_type = self._detect_view_type(nodes_for_detection)
        
        lines.append(f"=== VIEW === {view_type}")
        lines.append("")

        # ★追加: コマンドパレット特例処理
        # Viewが "command_palette" なら、MODALに残っている要素（入力欄やリスト）は
        # 実質的に「コンテンツ」なので、CONTENT領域に移動して専用フォーマッタを通す
        if view_type == "command_palette":
            regions.setdefault("CONTENT", []).extend(remaining_modal)
            remaining_modal = []

        # ---- 出力：順番固定 ----
        def _emit(title: str, nodes: List[Node], allow: Optional[Set[str]] = None, max_items: int = 18):
            chunk = self._compress_simple_list(nodes or [], allow_tags=allow, max_items=max_items)
            if chunk:
                lines.append(f"=== {title} ===")
                lines.extend(chunk)
                lines.append("")

        _emit("LAUNCHER", regions.get("APP_LAUNCHER", []), max_items=20)
        _emit("MENUBAR", regions.get("MENUBAR", []), allow={"menu", "push-button"}, max_items=12)
        _emit("ACTIVITY_BAR", regions.get("ACTIVITY_BAR", []), allow={"section", "push-button", "toggle-button", "static"}, max_items=14)
        _emit("SIDE_BAR", regions.get("SIDE_BAR", []), allow={"section", "tree-item", "list-item", "push-button", "heading", "label", "static", "text"}, max_items=18)
        _emit("TAB_BAR", regions.get("TAB_BAR", []), allow={"section", "push-button", "tab", "label", "static"}, max_items=10)
        _emit("BREADCRUMB", regions.get("BREADCRUMB", []), allow={"section", "label", "text", "link", "static"}, max_items=10)
        _emit("FIND / REPLACE", regions.get("FIND_REPLACE", []), allow={"entry", "check-box", "push-button", "label", "text", "section", "static"}, max_items=20)

        # CONTENT：viewごとに圧縮方針を変える
        content_lines = self._compress_content_by_view(regions.get("CONTENT", []), view_type=view_type)
        if content_lines:
            # タイトルも見やすく整形
            title_suffix = view_type.upper().replace("_", " ") if view_type != "generic" else "MAIN"
            lines.append(f"=== CONTENT ({title_suffix}) ===")
            lines.extend(content_lines)
            lines.append("")

        _emit("STATUSBAR", regions.get("STATUSBAR", []), allow={"push-button", "label", "text", "section", "static"}, max_items=14)

        # MODAL / NOTIFICATION（残ったものだけ）
        if remaining_modal:
            modal_lines = self._compress_simple_list(
                remaining_modal,
                allow_tags={"label", "text", "push-button", "link", "entry", "list-item", "section", "static"},
                max_items=22,
            )
            if modal_lines:
                lines.append("=== MODAL / NOTIFICATION ===")
                lines.extend(modal_lines)

        return lines