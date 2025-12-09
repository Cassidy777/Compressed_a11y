import re
from typing import List, Dict, Tuple, Set
from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    build_hierarchical_content_lines
)

class GimpCompressor(BaseA11yCompressor):
    domain_name = "gimp"
    
    # 【修正1】背景フィルタリングを無効化
    # 理由: berry.png (幅1628px) が「背景画像」と誤判定されて消えるのを防ぐため
    enable_background_filtering = False
    
    # 【修正2】ステータスバー自動抽出を無効化
    # 理由: id=7とid=8でタグが label <-> status で揺らぎ、MODAL誤検知の原因になるのを防ぐため
    use_statusbar = False

    MENU_KEYWORDS: Set[str] = {
        "file", "edit", "select", "view", "image", "layer", 
        "colors", "tools", "filters", "windows", "help"
    }

    def split_static_ui(self, nodes: List[Node], w: int, h: int) -> Tuple[List[Node], List[Node]]:
        regions = self.get_semantic_regions(nodes, w, h, dry_run=True)
        
        static_nodes = []
        dynamic_nodes = [] 
        
        static_groups = ["MENUBAR", "APP_LAUNCHER"]
        static_ids = set()
        for group in static_groups:
            for n in regions.get(group, []):
                static_ids.add(id(n))
        
        for n in nodes:
            if id(n) in static_ids:
                static_nodes.append(n)
            else:
                dynamic_nodes.append(n)
        
        # --- DEBUG TRACE: split_static_ui ---
        # berry.png が「動的(Dynamic)」として扱われているか確認
        # (動的であればDiff検出の対象になり、前画面と同じなら背景として扱われる)
        for n in nodes:
            if "berry" in (n.get("name") or n.get("text") or "").lower():
                status = "STATIC" if n in static_nodes else "DYNAMIC"
                print(f"[DEBUG TRACE] split_static_ui: 'berry' node classified as -> {status}")
        # ------------------------------------

        return dynamic_nodes, static_nodes

    def get_semantic_regions(
        self, nodes: List[Node], w: int, h: int, dry_run: bool = False
    ) -> Dict[str, List[Node]]:
        regions = {
            "MENUBAR": [],
            "APP_LAUNCHER": [],
            "TOOLBOX": [],
            "DOCKS": [],
            "CANVAS": [],
            "STATUSBAR": [],
            "MODAL": [],
        }

        LAUNCHER_X_LIMIT = w * 0.05
        MENU_Y_LIMIT = h * 0.10
        LEFT_PANEL_LIMIT = w * 0.22
        RIGHT_PANEL_START = w * 0.78
        STATUS_Y_START = h * 0.95

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            
            tag = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            name_lower = name.lower()

            # --- DEBUG TRACE: 領域判定の追跡 ---
            is_target = "berry" in name_lower
            if is_target:
                print(f"[DEBUG TRACE] Processing 'berry': Pos=({x},{y}) Size=({bw}x{bh}) Center=({cx},{cy}) Tag={tag}")
            # ----------------------------------

            # 1. APP_LAUNCHER
            if x < LAUNCHER_X_LIMIT and bw < w * 0.06 and bh > 30:
                if tag in ("push-button", "toggle-button"):
                    regions["APP_LAUNCHER"].append(n)
                    if is_target: print("  -> Assigned to APP_LAUNCHER")
                    continue
            if tag == "launcher-app":
                regions["APP_LAUNCHER"].append(n)
                if is_target: print("  -> Assigned to APP_LAUNCHER")
                continue

            # 2. MENUBAR
            if cy < MENU_Y_LIMIT:
                if tag == "menu" or name_lower in self.MENU_KEYWORDS:
                    regions["MENUBAR"].append(n)
                    if is_target: print("  -> Assigned to MENUBAR")
                    continue

            # 3. 浮動コンテナ (CANVASへ)
            if role in ("dialog", "alert", "window") or tag in ("window", "dialog"):
                regions["CANVAS"].append(n)
                if is_target: print("  -> Assigned to CANVAS (Rule: Role/Tag)")
                continue
            if tag in ("list-item", "table-cell", "menu-item"):
                regions["CANVAS"].append(n)
                if is_target: print("  -> Assigned to CANVAS (Rule: List/Table Item)")
                continue
            if tag == "push-button" and name_lower in ("ok", "cancel", "reset", "close", "help", "discard changes"):
                regions["CANVAS"].append(n)
                if is_target: print("  -> Assigned to CANVAS (Rule: Dialog Button)")
                continue

            # 4. TOOLBOX
            if cx < LEFT_PANEL_LIMIT:
                regions["TOOLBOX"].append(n)
                if is_target: print("  -> Assigned to TOOLBOX")
                continue

            # 5. DOCKS
            if cx > RIGHT_PANEL_START:
                regions["DOCKS"].append(n)
                if is_target: print("  -> Assigned to DOCKS")
                continue

            # 6. STATUSBAR (下部) # berry.png (ファイル名) はここに配置されることが多い 
            if cy > STATUS_Y_START: 
                regions["STATUSBAR"].append(n) 
                continue

            # 7. CANVAS
            regions["CANVAS"].append(n)
            if is_target: print("  -> Assigned to CANVAS (Rule: Default/Center)")



        return regions

    def _build_output(self, regions, modal_nodes, w, h) -> List[str]:
        lines = []
        lines.extend(self.get_meta_header(regions))
        
        modal_ids = {id(n) for n in modal_nodes} if modal_nodes else set()

        def filter_modal(nodes, region_name="Unknown"):
            filtered = []
            for n in nodes:
                if id(n) not in modal_ids:
                    filtered.append(n)
                else:
                    # --- DEBUG TRACE: MODAL化の確認 ---
                    if "berry" in (n.get("name") or "").lower():
                        print(f"[DEBUG TRACE] 'berry' was DETECTED AS MODAL (Removed from {region_name})")
                    # ----------------------------------
            return filtered

        if "WINDOW_CONTROLS" in regions:
            lines.append("WINDOW_CONTROLS:")
            lines.extend(self.process_region_lines(filter_modal(regions["WINDOW_CONTROLS"]), w, h))
        
        if regions["APP_LAUNCHER"]:
            lines.append("APP_LAUNCHER:")
            lines.extend(self.process_region_lines(filter_modal(regions["APP_LAUNCHER"]), w, h))

        if regions["MENUBAR"]:
            lines.append("MENUBAR:")
            lines.extend(self.process_region_lines(filter_modal(regions["MENUBAR"]), w, h))

        toolbox_nodes = filter_modal(regions["TOOLBOX"], "TOOLBOX")
        if toolbox_nodes:
            lines.append("TOOLBOX (Left Panel):")
            lines.extend(self.process_panel_lines(toolbox_nodes, w, h))

        # --- CANVAS (原因究明と出力) ---
        canvas_nodes = filter_modal(regions["CANVAS"], "CANVAS")
        if canvas_nodes:
            # ★DEBUG: 犯人捜しコード
            # もし背景フィルタがONだったら、berry.pngは消えていたか？をチェック
            self.check_background_filter_behavior(canvas_nodes, w, h)
            
            lines.append("CANVAS (Center):")
            # 修正済み(enable_background_filtering=False)なので、ここでは消えないはず
            lines.extend(self.process_content_lines(canvas_nodes, w, h))

        docks_nodes = filter_modal(regions["DOCKS"], "DOCKS")
        if docks_nodes:
            lines.append("DOCKS (Right Panel):")
            lines.extend(self.process_panel_lines(docks_nodes, w, h))

        # ★ STATUSBAR 出力部分を修正
        statusbar_nodes = filter_modal(regions.get("STATUSBAR", []), "STATUSBAR")
        if statusbar_nodes:
            lines.append("STATUSBAR:")
            raw_status_lines = self.process_region_lines(statusbar_nodes, w, h)

            cleaned_status_lines: List[str] = []
            status_pattern = re.compile(r'^\[status\] "(.*?)"')

            for ln in raw_status_lines:
                m = status_pattern.match(ln)
                if m:
                    text_inside = m.group(1).strip()
                    # 中身が完全に空なら捨てる
                    if not text_inside:
                        # print(f"[DEBUG] drop empty status line: {ln!r}")
                        continue
                cleaned_status_lines.append(ln)

            lines.extend(cleaned_status_lines)

        if modal_nodes:
            lines.append("MODAL:")
            lines.extend(self.process_modal_nodes(modal_nodes))
            
        return lines

    def check_background_filter_behavior(self, nodes: List[Node], w: int, h: int):
        """
        デバッグ用: 背景フィルタが有効だった場合の挙動をシミュレートしてログに出す。
        """
        target = next((n for n in nodes if "berry" in (n.get("name") or n.get("text") or "").lower()), None)
        
        if target:
            # 強制的にフィルタONの状態でテスト
            filtered_result = self._filter_background_noise([target])
            
            print(f"[DEBUG INVESTIGATION] Target 'berry' found in CANVAS nodes.")
            if not filtered_result:
                print(f"  -> [ROOT CAUSE CONFIRMED] With 'enable_background_filtering=True', this node WOULD BE DELETED.")
                print(f"  -> Reason: Node is too large/wide (treated as background image).")
                print(f"  -> Fix: 'enable_background_filtering=False' is now applied.")
            else:
                print(f"  -> This node passes the background filter (Issue might be elsewhere).")

    def process_panel_lines(self, nodes: List[Node], w: int, h: int) -> List[str]:
        # (変更なし)
        import re
        tuples = self._nodes_to_tuples(nodes)
        tuples.sort()
        merged_tuples = []
        skip_next = False
        Y_DIST_LIMIT = 40
        X_DIST_LIMIT = 80

        for i in range(len(tuples)):
            if skip_next:
                skip_next = False
                continue
            curr_y, curr_x, curr_line = tuples[i]
            if i == len(tuples) - 1:
                merged_tuples.append(tuples[i])
                break
            next_y, next_x, next_line = tuples[i+1]
            
            is_curr_label = any(tag in curr_line for tag in ["[label]", "[static]", "[text]"])
            is_next_input = any(tag in next_line for tag in ["[spin-button]", "[combo-box]", "[entry]", "[text]", "[toggle-button]"])
            is_vertical_neighbor = (0 <= next_y - curr_y <= Y_DIST_LIMIT) and (abs(next_x - curr_x) < X_DIST_LIMIT)
            
            if is_curr_label and is_next_input and is_vertical_neighbor:
                curr_text = self._extract_text_content(curr_line)
                next_text = self._extract_text_content(next_line)
                if curr_text and next_text:
                    clean_label = curr_text.rstrip(":, ").strip()
                    new_line = next_line.replace(f'"{next_text}"', f'"{clean_label}: {next_text}"')
                    merged_tuples.append((curr_y, curr_x, new_line))
                    skip_next = True
                    continue
            merged_tuples.append(tuples[i])

        return build_hierarchical_content_lines(merged_tuples)

    def _extract_text_content(self, line: str) -> str:
        import re
        m = re.search(r' "(.*?)" ', line)
        return m.group(1) if m else ""