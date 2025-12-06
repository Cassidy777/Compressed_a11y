from typing import List, Dict, Tuple, Set
from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    build_hierarchical_content_lines
)

class GimpCompressor(BaseA11yCompressor):
    domain_name = "gimp"
    
    # 背景フィルタリングを無効化 (berry.pngを残すため)
    enable_background_filtering = False
    
    # ステータスバーは自前で制御するためTrueにしておくが、
    # extract_system_ui をオーバーライドして自動吸い上げは防ぐ
    use_statusbar = True

    MENU_KEYWORDS: Set[str] = {
        "file", "edit", "select", "view", "image", "layer", 
        "colors", "tools", "filters", "windows", "help"
    }

    def extract_system_ui(self, nodes: List[Node], w: int, h: int) -> Tuple[List[Node], List[Node], List[Node]]:
        """
        親クラスの自動抽出を無効化する。
        GIMPでは画面下部の要素(ファイル名など)がコンテンツとして重要なため、
        勝手に status_nodes に吸い上げられて main_nodes から消えるのを防ぐ。
        """
        # launcher, status, main
        # ランチャーだけは親クラスのロジックで分離しても良いが、
        # ここではget_semantic_regionsで一括管理するため、すべてmainとして返す
        return [], [], nodes

    def split_static_ui(self, nodes: List[Node], w: int, h: int) -> Tuple[List[Node], List[Node]]:
        regions = self.get_semantic_regions(nodes, w, h, dry_run=True)
        
        static_nodes = []
        dynamic_nodes = [] 
        
        # 【修正】静的グループに TOOLBOX, DOCKS, STATUSBAR を追加
        # これにより、これらの領域にある berry.png 等が MODAL 候補になるのを防ぐ
        static_groups = ["MENUBAR", "APP_LAUNCHER", "TOOLBOX", "DOCKS", "STATUSBAR"]
        static_ids = set()
        for group in static_groups:
            for n in regions.get(group, []):
                static_ids.add(id(n))
        
        for n in nodes:
            if id(n) in static_ids:
                static_nodes.append(n)
            else:
                dynamic_nodes.append(n)
        
        # --- DEBUG TRACE ---
        for n in nodes:
            name = (n.get("name") or n.get("text") or "").lower()
            if "berry" in name:
                status = "STATIC" if n in static_nodes else "DYNAMIC"
                # print(f"[DEBUG TRACE] split_static_ui: '{name}' classified as -> {status}")
        # -------------------

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
            "STATUSBAR": [], # 追加
            "MODAL": [],
        }

        LAUNCHER_X_LIMIT = w * 0.05
        MENU_Y_LIMIT = h * 0.10
        LEFT_PANEL_LIMIT = w * 0.22
        RIGHT_PANEL_START = w * 0.78
        # ステータスバー判定用 (画面下部5%程度)
        STATUS_Y_START = h * 0.95 

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            
            tag = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            name_lower = name.lower()

            # 1. APP_LAUNCHER (左端)
            if x < LAUNCHER_X_LIMIT and bw < w * 0.06 and bh > 30:
                if tag in ("push-button", "toggle-button"):
                    regions["APP_LAUNCHER"].append(n)
                    continue
            if tag == "launcher-app":
                regions["APP_LAUNCHER"].append(n)
                continue

            # 2. MENUBAR (上部)
            if cy < MENU_Y_LIMIT:
                if tag == "menu" or name_lower in self.MENU_KEYWORDS:
                    regions["MENUBAR"].append(n)
                    continue

            # 3. 浮動コンテナ (CANVASへ優先配置)
            # ダイアログなどは位置に関わらず CANVAS (MODAL候補) 扱いにする
            is_dialog = False
            if role in ("dialog", "alert", "window") or tag in ("window", "dialog"):
                is_dialog = True
            elif tag == "push-button" and name_lower in ("ok", "cancel", "reset", "close", "help", "discard changes"):
                # ダイアログボタンっぽいものはパネル内であってもCanvas(Modal候補)として扱う
                is_dialog = True
            
            if is_dialog:
                regions["CANVAS"].append(n)
                continue

            # 4. STATUSBAR (下部)
            # berry.png (ファイル名) はここに配置されることが多い
            if cy > STATUS_Y_START:
                regions["STATUSBAR"].append(n)
                continue

            # 5. TOOLBOX (左パネル)
            if cx < LEFT_PANEL_LIMIT:
                regions["TOOLBOX"].append(n)
                continue

            # 6. DOCKS (右パネル)
            if cx > RIGHT_PANEL_START:
                regions["DOCKS"].append(n)
                continue

            # 7. CANVAS (中央)
            regions["CANVAS"].append(n)

        return regions

    def _build_output(self, regions, modal_nodes, w, h) -> List[str]:
        lines = []
        lines.extend(self.get_meta_header(regions))
        
        modal_ids = {id(n) for n in modal_nodes} if modal_nodes else set()

        def filter_modal(nodes):
            return [n for n in nodes if id(n) not in modal_ids]

        if "WINDOW_CONTROLS" in regions:
            lines.append("WINDOW_CONTROLS:")
            lines.extend(self.process_region_lines(filter_modal(regions["WINDOW_CONTROLS"]), w, h))
        
        if regions["APP_LAUNCHER"]:
            lines.append("APP_LAUNCHER:")
            lines.extend(self.process_region_lines(filter_modal(regions["APP_LAUNCHER"]), w, h))

        if regions["MENUBAR"]:
            lines.append("MENUBAR:")
            lines.extend(self.process_region_lines(filter_modal(regions["MENUBAR"]), w, h))

        toolbox_nodes = filter_modal(regions["TOOLBOX"])
        if toolbox_nodes:
            lines.append("TOOLBOX (Left Panel):")
            lines.extend(self.process_panel_lines(toolbox_nodes, w, h))

        canvas_nodes = filter_modal(regions["CANVAS"])
        if canvas_nodes:
            lines.append("CANVAS (Center):")
            lines.extend(self.process_content_lines(canvas_nodes, w, h))

        docks_nodes = filter_modal(regions["DOCKS"])
        if docks_nodes:
            lines.append("DOCKS (Right Panel):")
            lines.extend(self.process_panel_lines(docks_nodes, w, h))

        # 【修正】STATUSBAR の出力を追加
        statusbar_nodes = filter_modal(regions["STATUSBAR"])
        if statusbar_nodes:
            lines.append("STATUSBAR:")
            lines.extend(self.process_region_lines(statusbar_nodes, w, h))

        if modal_nodes:
            lines.append("MODAL:")
            lines.extend(self.process_modal_nodes(modal_nodes))
            
        return lines

    def process_panel_lines(self, nodes: List[Node], w: int, h: int) -> List[str]:
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