import re
from typing import List, Dict, Tuple, Set, Optional  # ★Optionalを追加

from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    build_hierarchical_content_lines
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
        MENU_Y_LIMIT = h * 0.07

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
            # Calc の UI では、"table-cell", "list-item" などが SHEET 内に来る
            if tag in ("table-cell", "list-item", "listitem", "table"):
                regions["SHEET"].append(n)
                continue

            # 数字だけのセル（例: "12345"）や ID らしきものも SHEET 側へ
            if tag == "text" and name.isdigit():
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


    def _select_sheet_nodes_relevant_to_instruction(
        self,
        sheet_nodes: List[Node],
    ) -> List[Node]:
        """
        SHEET 領域のノードから、
        - Instruction に出てきたヘッダ名（'Old ID' など）
        - 「xxx column」表現（Gross profit column 等）
        - セル範囲（B1:E30 等）
        を手がかりに、関連列・範囲に属するセルだけを返す。

        何も手がかりがない / マッチしない場合は、
        - 非空セルだけ返す（完全に空の行・列は削る）
        """
        if not sheet_nodes:
            return []

        # table-cell だけ抜き出し & インデックス化
        cells = []
        for n in sheet_nodes:
            tag = (n.get("tag") or "").lower()
            if tag != "table-cell":
                continue

            # 圧縮時に付けた cell_addr があれば優先
            addr = (n.get("cell_addr") or n.get("name") or "").strip()

            # "C1 : Old ID" 形式の場合、左側だけ番地として使う
            if ":" in addr:
                addr = addr.split(":", 1)[0].strip()

            col, row = parse_cell_addr(addr)
            if col is None:
                continue

            text = (n.get("text") or "").strip()
            cells.append({"node": n, "col": col, "row": row, "text": text})

        if not cells:
            # まともなセル番地が取れなければ、そのまま返す
            return sheet_nodes

        # 最上段をヘッダ行とみなす（通常は row=1）
        header_row = min(c["row"] for c in cells)

        # 列ごとのヘッダ文字列を構築（"Old ID" 等）
        headers: Dict[str, str] = {}
        for c in cells:
            if c["row"] != header_row:
                continue
            node = c["node"]
            name = (node.get("name") or "").strip()
            header_text = ""

            # "C1 : Old ID" -> "Old ID"
            if ":" in name:
                header_text = name.split(":", 1)[1].strip()
            else:
                header_text = (node.get("text") or "").strip()

            if header_text:
                headers[c["col"]] = header_text.lower()

        # --- Instruction から列ヒントを集める ---
        summary = self.instruction_summary or {}
        col_hints = summary.get("column_hints") or {}
        header_terms: Set[str] = set(
            t.lower() for t in col_hints.get("header_terms", set())
        )

        # 'Old ID' などクォート内の語も header 候補に追加
        for qt in summary.get("quoted_terms") or []:
            header_terms.add(qt.lower())

        target_cols: Set[str] = set()

        # 1) ヘッダ名でマッチする列
        for col, htext in headers.items():
            for term in header_terms:
                if not term:
                    continue
                if term in htext or htext in term:
                    target_cols.add(col)
                    break

        # 2) "column A", "columns B to E" などの明示列
        letters = col_hints.get("letters") or set()
        for L in letters:
            target_cols.add(L.upper())

        # 3) B1:E30 などの範囲から列を推定
        for start, end in summary.get("cell_ranges") or []:
            # ★修正: クラスメソッドではなくグローバル関数を使うため self. を削除
            scol, _ = parse_cell_addr(start)
            ecol, _ = parse_cell_addr(end)
            if not scol or not ecol:
                continue
            for col in iter_col_range(scol, ecol):
                target_cols.add(col)

        # --- ターゲット列がわかっている場合 ---
        if target_cols:
            # 「ターゲット列の中で、一番下まで実データがある行」を求める
            max_nonempty_row = header_row
            for c in cells:
                if c["col"] in target_cols:
                    content = (c["text"] or "").strip()
                    if content and c["row"] > max_nonempty_row:
                        max_nonempty_row = c["row"]

            selected_nodes: List[Node] = []
            for c in cells:
                if c["col"] in target_cols and c["row"] <= max_nonempty_row:
                    selected_nodes.append(c["node"])

            if selected_nodes:
                return selected_nodes

        # --- ターゲット列が特定できない場合: 非空セルのみ残す ---
        non_empty_nodes: List[Node] = []
        for c in cells:
            content = (c["text"] or "").strip()
            if content:
                non_empty_nodes.append(c["node"])

        if non_empty_nodes:
            return non_empty_nodes

        # それでも何もなければ元のノードをそのまま返す
        return sheet_nodes



    def _build_output(self, regions, modal_nodes, w, h) -> List[str]:
        """
        LibreOffice Calc 用の出力構築ロジック。
        """
        lines = []
        lines.extend(self.get_meta_header(regions))

        # MODAL ノードの ID セット
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
        if regions.get("MENUBAR"):
            lines.append("MENUBAR:")
            lines.extend(self.process_region_lines(
                filter_modal(regions["MENUBAR"], "MENUBAR"),
                w, h
            ))

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
            lines.append("MODAL:")
            lines.extend(self.process_modal_nodes(modal_nodes))

        return lines