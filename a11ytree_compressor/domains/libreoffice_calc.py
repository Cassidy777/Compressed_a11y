import re
from typing import List, Dict, Tuple, Set, Optional  # ★Optionalを追加
from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    build_hierarchical_content_lines, dedup_horizontal_menu_nodes
)
from ..a11y_instruction_utils import summarize_calc_instruction

CELL_ADDR_RE = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})$")

# ★修正: | None ではなく Optional[...] を使用
def parse_cell_addr(addr: str) -> Tuple[Optional[str], Optional[int]]:
    """
    "C12" -> ("C", 12) のように分解する。
    マッチしなければ (None, None)。
    """
    if not addr:
        return None, None
    m = CELL_ADDR_RE.match(addr.strip())
    if not m:
        return None, None
    col, row_str = m.groups()
    try:
        return col.upper(), int(row_str)
    except ValueError:
        return None, None


def col_to_index(col: str) -> int:
    """ "A" -> 1, "B" -> 2, ..., "AA" -> 27 """
    col = col.upper()
    idx = 0
    for ch in col:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx


def index_to_col(idx: int) -> str:
    """ 1 -> "A", 2 -> "B", ..., 27 -> "AA" """
    chars = []
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def iter_col_range(start: str, end: str) -> List[str]:
    """ "B","E" -> ["B","C","D","E"] """
    s = col_to_index(start)
    e = col_to_index(end)
    if s > e:
        s, e = e, s
    return [index_to_col(i) for i in range(s, e + 1)]



class LibreOfficeCalcCompressor(BaseA11yCompressor):
    domain_name = "libreoffice_calc"
    
    enable_background_filtering = False
    use_statusbar = False

    MENU_KEYWORDS: Set[str] = {
        "file", "edit", "view", "insert", "format", "styles",
        "sheet", "data", "tools", "window", "help"
    }

    MODAL_KEYWORDS: Set[str] = {
        "document in use",
        # 必要なら他のモーダルタイトルも追加
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Calc 用 instruction 要約を保持する
        self.instruction_summary: Dict[str, object] = {
            "quoted_terms": [],
            "cell_ranges": [],
            "cell_refs": [],
            "column_hints": {"header_terms": set(), "letters": set()},
        }

        # ★ デバッグ用フラグ（必要に応じて False にすれば静かになる）
        self.debug_modal: bool = True

        # フレームカウンタ（どのステップかをざっくり追うため）
        self._frame_idx_for_debug: int = 0

    # --- デバッグ用の小さいヘルパー ---

    def _dbg_modal(self, msg: str) -> None:
        """Calc 用モーダルデバッグ出力"""
        if getattr(self, "debug_modal", False):
            print(f"[DEBUG CALC MODAL] {msg}")

    def _format_node_short(self, n: Node) -> str:
        tag = (n.get("tag") or "").lower()
        name = (n.get("name") or n.get("text") or "").strip()
        bbox = node_bbox_from_raw(n)
        return f"{tag}:'{name}'@({bbox['x']},{bbox['y']})"


    def compress(self, nodes, instruction=None, **kwargs):
        """
        BaseA11yCompressor.compress と同じシグネチャでオーバーライド。
        instruction を先に要約して self に持たせてから、
        共通パイプラインに流す。
        """
        self.instruction_summary = summarize_calc_instruction(instruction or "")
        return super().compress(nodes, instruction=instruction, **kwargs)



    def split_static_ui(self, nodes: List[Node], w: int, h: int) -> Tuple[List[Node], List[Node]]:
        regions = self.get_semantic_regions(nodes, w, h, dry_run=True)
        
        static_nodes = []
        dynamic_nodes = [] 
        
        # 修正: TOOLBAR, STATUSBAR, SHEET_TABS も静的要素として扱うことで圧縮効率を向上
        static_groups = ["MENUBAR", "APP_LAUNCHER", "TOOLBAR", "STATUSBAR", "SHEET_TABS"]
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
        # 修正: "TOOLBAR" キーを追加（これがないと KeyError になる）
        regions = {
            "MENUBAR": [],
            "APP_LAUNCHER": [],
            "TOOLBAR": [],
            "SHEET": [],
            "SHEET_TABS": [],
            "STATUSBAR": [],
            "MODAL": [],
        }

        LAUNCHER_X_LIMIT = w * 0.05
        MENU_Y_LIMIT = h * 0.09

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
                if tag in ("push-button", "toggle-button"):
                    regions["APP_LAUNCHER"].append(n)
                    continue
            if tag == "launcher-app":
                regions["APP_LAUNCHER"].append(n)
                continue

            # 2. MENUBAR
            if cy < MENU_Y_LIMIT:
                if name_lower in self.MENU_KEYWORDS:
                    regions["MENUBAR"].append(n)
                    continue

            # 3. MODAL（Calc は "Document in Use" など）
            if any(key in name_lower for key in self.MODAL_KEYWORDS):
                regions["MODAL"].append(n)
                continue

            # ===========================
            # 4. SHEET（表のセル・行・列）
            # ===========================
            # 「番地付きセル(table-cell)」だけを SHEET に入れる
            if self._is_sheet_like_cell(n, h):
                regions["SHEET"].append(n)
                continue

            # 数字だけのセル（例: "12345"）や ID らしきものも SHEET 側へ
            if tag == "text":
                name = (n.get("name") or n.get("text") or "").strip()
                if name.isdigit():
                    regions["SHEET"].append(n)
                    continue

            # ===========================
            # 5. SHEET_TABS（Calc の下部）
            # ===========================
            # y が画面下部 & タブ系の名称 ("Sheet1", "Sheet2", "+")
            if cy > h * 0.90:
                if name_lower.startswith("sheet") or name_lower in ("+", "add", "new sheet"):
                    regions["SHEET_TABS"].append(n)
                    continue

            # ===========================
            # 6. STATUSBAR（さらに下部）
            # ===========================
            # Statusbar は Calc では画面最下段
            if cy > h * 0.95:
                regions["STATUSBAR"].append(n)
                continue

            # ===========================
            # 7. TOOLBAR（中央上段・メニューの下）
            # ===========================
            # y が MENUBAR 下 + 特定の role を持つもの
            if MENU_Y_LIMIT < cy < h * 0.25:
                # ボタン系ウィジェット
                if tag in ("push-button", "toggle-button", "combo-box", "entry", "textbox"):
                    regions["TOOLBAR"].append(n)
                    continue

            # フォント名・サイズ・アクティブセルなど、ツールバーと一体になっているテキスト
            if MENU_Y_LIMIT < cy < h * 0.30 and tag == "text":
                regions["TOOLBAR"].append(n)
                continue

            # ===========================
            # 8. その他 → SHEET（Calc は中央がほぼすべてシート）
            # ===========================
            regions["SHEET"].append(n)

        return regions


    def _estimate_target_cols(self, cells: List[Dict], header_row: int) -> Set[str]:
        """
        Instruction とセル情報から「ターゲットとなる列（Source & Destination）」を推定する。
        """
        target_cols = set()

        # 1. 列ごとのヘッダ文字列を構築
        headers: Dict[str, str] = {}
        for c in cells:
            if c["row"] != header_row:
                continue
            
            node_name = (c["node"].get("name") or "").strip()
            if ":" in node_name:
                header_text = node_name.split(":", 1)[1].strip()
            else:
                header_text = c["text"]
            
            if header_text:
                headers[c["col"]] = header_text.lower()

        # 2. Instruction からヒントを取得
        summary = self.instruction_summary or {}
        col_hints = summary.get("column_hints") or {}
        
        # 検索語リスト: hintのheader_terms + quoted_terms
        header_terms: Set[str] = set(
            t.lower() for t in col_hints.get("header_terms", set())
        )
        for qt in summary.get("quoted_terms") or []:
            header_terms.add(qt.lower())

        # ★追加修正: コンテキストとして重要な列（Customers, Name等）は
        # 指示になくても常に target_cols に含めるための「デフォルトキーワード」
        # これにより "Customers" 列などが自動的に残るようになる
        default_context_keywords = {"customer", "name", "id", "label"}
        header_terms.update(default_context_keywords)

        # 3. マッチング処理
        
        # A) ヘッダ名マッチ
        for col, htext in headers.items():
            for term in header_terms:
                if not term:
                    continue
                if term in htext or htext in term:
                    target_cols.add(col)
                    break
        
        # B) 明示的な列指定
        letters = col_hints.get("letters") or set()
        for L in letters:
            target_cols.add(L.upper())

        # C) 範囲指定からの推定
        for start, end in summary.get("cell_ranges") or []:
            scol, _ = parse_cell_addr(start)
            ecol, _ = parse_cell_addr(end)
            if not scol or not ecol:
                continue
            for col in iter_col_range(scol, ecol):
                target_cols.add(col)

        return target_cols


    def _select_sheet_nodes_relevant_to_instruction(
        self,
        sheet_nodes: List[Node],
        instruction: Optional[str] = None,
    ) -> List[Node]:
        """
        【改善版戦略 v2】
        1. 「完全に空の列（A列など）」は除外。
           ※ textが `""` や `"` だけの場合も「空」とみなす厳格なチェックを導入。
        2. 「データが存在する最終行」を特定し、それより下の無駄な空行をカット。
        """
        if not sheet_nodes:
            return []

        # 1. table-cell のみを抽出してメタデータ化
        cells = []
        for n in sheet_nodes:
            tag = (n.get("tag") or "").lower()
            if tag != "table-cell":
                continue
            
            addr = (n.get("cell_addr") or n.get("name") or "").strip()
            if ":" in addr: 
                addr = addr.split(":", 1)[0].strip()
            
            col, row = parse_cell_addr(addr)
            if col is None or row is None:
                continue
            
            # テキストのクリーニング
            raw_text = (n.get("text") or "").strip()
            cells.append({
                "node": n, 
                "col": col, 
                "row": row, 
                "text": raw_text
            })

        if not cells:
            return sheet_nodes

        # 2. ヘッダ行の特定
        header_row = min(c["row"] for c in cells)

        # 3. valid_cols & max_content_row の特定（厳格モード）
        valid_cols = set()
        max_content_row = header_row

        for c in cells:
            text = c["text"]
            name = (c["node"].get("name") or "").strip()
            
            # ヘッダ名抽出 ("C1 : Old ID" -> "Old ID")
            header_from_name = ""
            if ":" in name:
                parts = name.split(":", 1)
                if len(parts) > 1:
                    header_from_name = parts[1].strip()
            
            # ★修正: 「ゴミ」テキストを無視する判定関数
            # text が空文字、または `""` という記号のみの場合は「データなし」とみなす
            def is_meaningful(t):
                if not t: return False
                if t == '""' or t == '"': return False
                return True

            # テキスト または ヘッダ名 が「意味のある内容」なら有効
            has_content = is_meaningful(text) or is_meaningful(header_from_name)

            if has_content:
                valid_cols.add(c["col"])
                if c["row"] > max_content_row:
                    max_content_row = c["row"]

        # データがある最終行 + 1行（予備）までを残す
        cutoff_row = max_content_row + 1

        # 4. 関連列 (target_cols) を推定
        target_cols = self._estimate_target_cols(cells, header_row)

        # 5. ノードのフィルタリング
        selected_nodes = []
        
        for c in cells:
            # Rule 0: 無効な列（A列/E〜T列）ならスキップ
            if c["col"] not in valid_cols:
                continue

            # Rule X: データ終了行より下の無駄な空行はスキップ
            if c["row"] > cutoff_row:
                continue

            # Rule A: ヘッダ行なら無条件で残す
            if c["row"] == header_row:
                selected_nodes.append(c["node"])
                continue

            # Rule B: データ行の選定
            if target_cols:
                # ★ target_cols を“優先して含める”だけにする
                if c["col"] in target_cols:
                    selected_nodes.append(c["node"])
                else:
                    # ★ valid_cols に入っていれば残す
                    if c["col"] in valid_cols:
                        selected_nodes.append(c["node"])
            else:
                # target_cols が無いときは従来の fallback
                if c["text"] and c["text"] != '""':
                    selected_nodes.append(c["node"])

        # 保険
        if not selected_nodes:
            return [c["node"] for c in cells if c["row"] == header_row and c["col"] in valid_cols]

        return selected_nodes




    def _filter_modal_nodes(self, modal_nodes: List[Node], w: int, h: int) -> List[Node]:
        """
        Calc 用:
        Delete Contents ダイアログ表示時などに、
        メニューバーとほぼ同じ menu ノードが MODAL 側にも複製される。

        - 画面上部 (y が小さい)
        - tag == "menu"
        - ラベルが File/Edit/View/... などメニューバー項目
        というノードは、MODAL 内容には不要なので除外する。
        """
        filtered: List[Node] = []
        TOP_LIMIT = h * 0.20  # 画面上部 20% くらいまでを「メニューバー領域」とみなす

        for n in modal_nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            name_lower = name.lower()

            bbox = node_bbox_from_raw(n)
            y = bbox["y"]

            # 上部に並んだ File/Edit/... の menu は、MODAL からは除外
            if (
                tag == "menu"
                and name_lower in self.MENU_KEYWORDS
                and y < TOP_LIMIT
            ):
                continue

            filtered.append(n)

        return filtered
    
    def _is_sheet_like_cell(self, node: Node, h: int) -> bool:
        """
        MODAL に入ってしまった Node のうち、
        本来は SHEET に属していそうな「セルっぽい」ものかどうかを判定する。

        - tag == "table-cell"
        - name/cell_addr が A1, B12 などのセル番地
        を SHEET 寄りとして扱う。
        """
        tag = (node.get("tag") or "").lower()
        if tag != "table-cell":
            return False

        # 上の方にあるツールバーや見出し行を誤って拾いすぎないための軽いガード
        bbox = node_bbox_from_raw(node)
        y = bbox["y"]
        if y < h * 0.15:
            # 画面のかなり上にあるものは SHEET ではなくヘッダ系の可能性が高いので除外
            return False

        # ★重要: セル番地は text ではなく name / cell_addr に入っている
        addr = (node.get("cell_addr") or node.get("name") or "").strip()
        if not addr:
            return False

        # 念のため "A1: something" みたいな状態なら ":" までで切る
        if ":" in addr:
            addr = addr.split(":", 1)[0].strip()

        col, row = parse_cell_addr(addr)
        if col is not None and row is not None:
            return True

        return False



    def _build_output(self, regions, modal_nodes, w, h) -> List[str]:
        """
        LibreOffice Calc 用の出力構築ロジック。
        """
        
        lines = []
        lines.extend(self.get_meta_header(regions))

        # -----------------------------------------------------
        # Step 1: MODAL に紛れ込んだ「シートセル」を SHEET に救済
        # -----------------------------------------------------
        if modal_nodes and regions.get("SHEET") is not None:
            rescued_cells: List[Node] = []
            kept_modal: List[Node] = []

            for n in modal_nodes:
                if self._is_sheet_like_cell(n, h):
                    rescued_cells.append(n)
                else:
                    kept_modal.append(n)

            if rescued_cells:
                regions["SHEET"] = list(regions.get("SHEET") or []) + rescued_cells

            modal_nodes = kept_modal

        modal_ids = {id(n) for n in modal_nodes} if modal_nodes else set()

        def filter_modal(nodes, region_name="Unknown"):
            """MODAL 領域と重複するノードを除外"""
            filtered = []
            for n in nodes:
                if id(n) not in modal_ids:
                    filtered.append(n)
            return filtered

        # =====================================================
        # APP_LAUNCHER
        # =====================================================
        if regions.get("APP_LAUNCHER"):
            lines.append("APP_LAUNCHER:")
            lines.extend(self.process_region_lines(
                filter_modal(regions["APP_LAUNCHER"], "APP_LAUNCHER"),
                w, h
            ))

        # =====================================================
        # MENUBAR
        # =====================================================
        menubar_nodes = filter_modal(regions.get("MENUBAR", []), "MENUBAR")
        if menubar_nodes:
            # 共通の「横並びメニュー dedup」処理を適用
            menubar_nodes = dedup_horizontal_menu_nodes(menubar_nodes)

            lines.append("MENUBAR:")
            lines.extend(self.process_region_lines(menubar_nodes, w, h))

        # =====================================================
        # TOOLBAR
        # =====================================================
        if regions.get("TOOLBAR"):
            lines.append("TOOLBAR:")
            lines.extend(self.process_region_lines(
                filter_modal(regions["TOOLBAR"], "TOOLBAR"),
                w, h
            ))

        # =====================================================
        # SHEET（Calc のメイン領域）
        # =====================================================
        sheet_nodes = filter_modal(regions.get("SHEET", []), "SHEET")
        if sheet_nodes:
            cleaned_sheet_nodes: List[Node] = []
            for n in sheet_nodes:
                tag = (n.get("tag") or "").lower()
                name = (n.get("name") or n.get("text") or "").strip()
                name_lower = name.lower()
                bbox = node_bbox_from_raw(n)
                y = bbox["y"]

                # MENUBAR キーワードっぽいものは SHEET から除外
                if name_lower in self.MENU_KEYWORDS and y < h * 0.2:
                    continue

                # 上部ツールバー帯にあるボタン／テキストも SHEET から除外
                if y < h * 0.3 and tag in (
                    "push-button", "toggle-button", "combo-box",
                    "entry", "textbox", "text"
                ):
                    continue

                # Document in Use 系のボタンもここで弾く（スクショモードでは不要）
                if tag == "push-button" and name_lower in (
                    "open read-only", "notify", "open", "cancel"
                ):
                    continue

                # ★ table-cell だけ "C2 : 76" のような表示に変換
                if tag == "table-cell":
                    cell_addr = (n.get("name") or "").strip()
                    cell_text = (n.get("text") or "").strip()

                    if cell_addr and cell_text:
                        display = f"{cell_addr} : {cell_text}"
                    elif cell_text:
                        display = cell_text
                    else:
                        display = cell_addr

                    new_n = dict(n)  # 元ノードを壊さない
                    if cell_addr:
                        new_n["cell_addr"] = cell_addr  # 後でコードからも使える
                    new_n["name"] = display          # process_content_lines は name を見る
                    cleaned_sheet_nodes.append(new_n)
                else:
                    cleaned_sheet_nodes.append(n)

            if cleaned_sheet_nodes:
                # ★ Instruction を使って、タスクに関係ありそうなセルだけに絞る
                selected_nodes = self._select_sheet_nodes_relevant_to_instruction(
                    cleaned_sheet_nodes
                )
                if selected_nodes:
                    lines.append("SHEET:")
                    lines.extend(self.process_content_lines(selected_nodes, w, h))

        # =====================================================
        # SHEET_TABS（画面下部のシート名）
        # =====================================================
        sheet_tab_nodes = filter_modal(regions.get("SHEET_TABS", []), "SHEET_TABS")
        if sheet_tab_nodes:
            lines.append("SHEET_TABS:")
            lines.extend(self.process_region_lines(sheet_tab_nodes, w, h))

        # =====================================================
        # STATUSBAR
        # =====================================================
        statusbar_nodes = filter_modal(regions.get("STATUSBAR", []), "STATUSBAR")
        if statusbar_nodes:
            lines.append("STATUSBAR:")
            raw_status_lines = self.process_region_lines(statusbar_nodes, w, h)

            # 空の status を除去
            cleaned_status_lines = []
            status_pattern = re.compile(r'^\[status\] "(.*?)"')

            for ln in raw_status_lines:
                m = status_pattern.match(ln)
                if m:
                    inside = m.group(1).strip()
                    if not inside:
                        continue
                cleaned_status_lines.append(ln)

            lines.extend(cleaned_status_lines)

        # =====================================================
        # MODAL
        # =====================================================
        if modal_nodes:
            # Calc 用フィルタで「メニューバーのコピー」を除外
            modal_nodes = self._filter_modal_nodes(modal_nodes, w, h)

        # もしメニューバー以外何も残らなかったら MODAL セクション自体を出さない
        if modal_nodes:
            lines.append("MODAL:")
            lines.extend(self.process_modal_nodes(modal_nodes))
        self._prev_modal_nodes_for_persist = list(modal_nodes) if modal_nodes else []
        return lines