import re
from typing import List, Dict, Tuple, Set, Optional, Any
from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    build_hierarchical_content_lines, dedup_horizontal_menu_nodes
)
from ..a11y_instruction_utils import summarize_calc_instruction


class OSCompressor(BaseA11yCompressor):
    domain_name = "os"
    
    # 背景削除は誤判定のリスクがあるためFalse、ステータスバーは情報源として使う
    enable_background_filtering = False
    use_statusbar = True 

    MODAL_KEYWORDS: Set[str] = {
        "authentication", "password", "required", "authenticate", "cancel"
    }

    def split_static_ui(self, nodes: List[Node], w: int, h: int) -> Tuple[List[Node], List[Node]]:
        """
        OS(ubuntu/gnome) 向け:
        - 毎回ほぼ固定で出てくる OS 要素を static とみなす
          -> APP_LAUNCHER / TOP_BAR / DESKTOP_ICONS / STATUSBAR
        - それ以外 (ウィンドウ内容やポップアップなど) を dynamic として扱う
        """
        regions = self.get_semantic_regions(nodes, w, h, dry_run=True)

        static_nodes: List[Node] = []
        dynamic_nodes: List[Node] = []

        static_groups = [
            "APP_LAUNCHER",
            "TOP_BAR",
            "DESKTOP_ICONS",
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


# サイドバー検出用のキーワード定義
    SIDEBAR_KEYWORDS_SPECIFIC = {"Recent", "Starred", "Other Locations"}
    SIDEBAR_KEYWORDS_GENERIC = {
        "Home", "Desktop", "Documents", "Downloads", 
        "Music", "Pictures", "Videos", "Trash",
        # Settings用に追加
        "Network", "Bluetooth", "Background", "Appearance", "Notifications",
        "Search", "Multitasking", "Applications", "Privacy", "Online Accounts",
        "Sharing", "Sound", "Power", "Displays", "Mouse & Touchpad",
        "Keyboard", "Printers", "Removable Media", "Color", "Region & Language",
        "Accessibility", "Users", "Date & Time", "About",
    }

    def _detect_sidebar_region(self, nodes: List[Node]) -> Optional[Dict[str, int]]:
        """
        （前回の修正と同じ：特定のキーワードを持つノードが縦に並んでいるパターンを検出）
        """
        candidates = []
        all_keywords = self.SIDEBAR_KEYWORDS_SPECIFIC | self.SIDEBAR_KEYWORDS_GENERIC
        
        for n in nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if name in all_keywords:
                candidates.append(n)

        if not candidates:
            return None

        # X座標でソート
        candidates.sort(key=lambda n: node_bbox_from_raw(n)["x"])

        best_cluster = []
        current_cluster = []
        
        for n in candidates:
            if not current_cluster:
                current_cluster.append(n)
                continue
            
            prev = current_cluster[0]
            bx = node_bbox_from_raw(n)["x"]
            px = node_bbox_from_raw(prev)["x"]
            
            if abs(bx - px) < 30: 
                current_cluster.append(n)
            else:
                if len(current_cluster) > len(best_cluster):
                    best_cluster = current_cluster
                current_cluster = [n]
        
        if len(current_cluster) > len(best_cluster):
            best_cluster = current_cluster
            
        if not best_cluster:
            return None

        names = {(n.get("name") or n.get("text") or "").strip() for n in best_cluster}
        has_specific = bool(names & self.SIDEBAR_KEYWORDS_SPECIFIC)
        
        is_valid = (has_specific and len(best_cluster) >= 2) or len(best_cluster) >= 4
        
        if is_valid:
            xs = [node_bbox_from_raw(n)["x"] for n in best_cluster]
            ys = [node_bbox_from_raw(n)["y"] for n in best_cluster]
            ws = [node_bbox_from_raw(n)["w"] for n in best_cluster]
            hs = [node_bbox_from_raw(n)["h"] for n in best_cluster]
            
            min_x, min_y = min(xs), min(ys)
            max_x = max(x + w for x, w in zip(xs, ws))
            max_y = max(y + h for y, h in zip(ys, hs))
            
            return {
                "x": min_x - 60,
                "y": min_y - 20,
                "w": (max_x - min_x) + 80,
                "h": (max_y - min_y) + 40
            }
        return None

    def _detect_breadcrumb_region(self, nodes: List[Node]) -> Optional[Dict[str, int]]:
        """
        ★新規追加: パンくずリスト（例: Home / project）を検出する。
        「/」などのセパレータを探し、その同じ高さ(Y座標)にある要素群を領域として返す。
        """
        # セパレータ候補
        separators = [n for n in nodes if (n.get("name") or "").strip() in {"/", ">", "›", "»"}]
        
        if not separators:
            return None

        # 最初のセパレータを基準にする
        ref = separators[0]
        ref_box = node_bbox_from_raw(ref)
        ref_cy = ref_box["y"] + ref_box["h"] / 2
        
        # 同じ高さ(Y軸)にあるノードを集める（パンくずの構成要素）
        cluster = []
        for n in nodes:
            b = node_bbox_from_raw(n)
            cy = b["y"] + b["h"] / 2
            # 高さの差が小さい（例えば20px以内）なら同じ行とみなす
            if abs(cy - ref_cy) < 20:
                cluster.append(n)
        
        if cluster:
            xs = [node_bbox_from_raw(n)["x"] for n in cluster]
            ys = [node_bbox_from_raw(n)["y"] for n in cluster]
            ws = [node_bbox_from_raw(n)["w"] for n in cluster]
            hs = [node_bbox_from_raw(n)["h"] for n in cluster]
            
            min_x, min_y = min(xs), min(ys)
            max_x = max(x + w for x, w in zip(xs, ws))
            max_y = max(y + h for y, h in zip(ys, hs))
            
            # 領域を少し広げて返す
            return {
                "x": min_x - 20,
                "y": min_y - 10,
                "w": (max_x - min_x) + 40,
                "h": (max_y - min_y) + 20
            }
        return None

    def _detect_window_content_regions(self, nodes: List[Node]) -> List[Dict[str, int]]:
        """
        ウィンドウ特有のウィジェットが存在する領域を特定する。
        """
        anchors = []
        
        # 1. Key-Valueペア検出 (Settingsウィンドウ右側対策)
        # ラベル要素だけを集めて、Y座標でソート
        labels = [n for n in nodes if (n.get("tag") or "").lower() == "label"]
        labels.sort(key=lambda n: node_bbox_from_raw(n)["y"])
        
        matched_kv_ids = set()

        for i, l1 in enumerate(labels):
            if id(l1) in matched_kv_ids: continue
            
            b1 = node_bbox_from_raw(l1)
            # l1 の右側にある l2 を探す
            for l2 in labels[i+1:]:
                if id(l2) in matched_kv_ids: continue
                
                b2 = node_bbox_from_raw(l2)
                # 高さのズレが10px以内
                if abs((b2["y"]+b2["h"]/2) - (b1["y"]+b1["h"]/2)) > 10: 
                    if b2["y"] > b1["y"] + b1["h"]: break # 下の行に行ったら終了
                    continue
                
                # 横位置チェック: l1の右側にあり、かつ近すぎず遠すぎない(300px以内)
                dist_x = b2["x"] - (b1["x"] + b1["w"])
                if 0 < dist_x < 300:
                    # Key-Valueペア発見
                    anchors.extend([l1, l2])
                    matched_kv_ids.update([id(l1), id(l2)])
                    break

        # 2. 通常アンカー検出
        for n in nodes:
            # 既にKV判定で追加されていればスキップ
            if id(n) in matched_kv_ids: continue

            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            
            # A. 明らかなウィンドウ構成部品
            if tag in {
                "check-box", "combo-box", "spin-button", "entry", 
                "terminal", "slider", "switch", "scroll-bar", "menu-bar",
                "table", "tree-table", "radio-button", "text"
            }:
                anchors.append(n)
            
            # B. ウィンドウ制御ボタン / ダイアログ特有のボタン
            #    "Help" ボタンはダイアログによくあるため追加
            elif tag in {"push-button", "toggle-button"}:
                if name in {"Close", "Minimize", "Maximize", "Help", "Cancel", "Apply", "OK"}:
                    anchors.append(n)
                
            # C. コンテナそのもの
            elif tag in {"window", "frame", "dialog"}:
                anchors.append(n)

            # D. ★追加: フォームのラベル（末尾がコロン）
            #    例: "Profile ID:", "Cursor shape:" などは設定画面の確実な証拠
            elif tag == "label" and name.endswith(":"):
                anchors.append(n)

        if not anchors:
            return []

        # アンカー要素をbbox化
        boxes = [node_bbox_from_raw(n) for n in anchors]
        
        # 距離が近いボックス同士をマージして「ウィンドウ領域」を作る
        merged_boxes = []
        while boxes:
            current = boxes.pop(0)
            changed = True
            while changed:
                changed = False
                remaining = []
                for other in boxes:
                    # ★変更: 許容距離を拡大 (150 -> 250)
                    # 左サイドバー(General)と右メインパネル(Checkboxes)の間には空白があるため、
                    # 広めのマージンを取って「1つの大きなウィンドウ」として認識させる
                    if self._boxes_are_close(current, other, tolerance=250):
                        current = self._merge_bbox(current, other)
                        changed = True
                    else:
                        remaining.append(other)
                boxes = remaining
            merged_boxes.append(current)
            
        return merged_boxes

    def _boxes_are_close(self, b1, b2, tolerance=100):
        # 2つの矩形が許容範囲内で重なる/近いか判定
        x_overlap = (b1["x"] <= b2["x"] + b2["w"] + tolerance) and (b2["x"] <= b1["x"] + b1["w"] + tolerance)
        y_overlap = (b1["y"] <= b2["y"] + b2["h"] + tolerance) and (b2["y"] <= b1["y"] + b1["h"] + tolerance)
        return x_overlap and y_overlap

    def _merge_bbox(self, b1, b2):
        min_x = min(b1["x"], b2["x"])
        min_y = min(b1["y"], b2["y"])
        max_x = max(b1["x"] + b1["w"], b2["x"] + b2["w"])
        max_y = max(b1["y"] + b1["h"], b2["y"] + b2["h"])
        return {"x": min_x, "y": min_y, "w": max_x - min_x, "h": max_y - min_y}

    def get_semantic_regions(
        self, nodes: List[Node], w: int, h: int, dry_run: bool = False
    ) -> Dict[str, List[Node]]:
        """
        OS(ubuntu/gnome) 向けのセマンティック分割。
        """
        regions: Dict[str, List[Node]] = {
            "APP_LAUNCHER": [],
            "TOP_BAR": [],
            "DESKTOP_ICONS": [],
            "OS_POPUP": [],
            "MODAL": [],
            "CONTENT": [],
        }

        LAUNCHER_X_LIMIT = w * 0.05
        TOP_BAR_MAX_Y    = h * 0.03
        
        sidebar_bbox = self._detect_sidebar_region(nodes)
        breadcrumb_bbox = self._detect_breadcrumb_region(nodes)
        window_regions = self._detect_window_content_regions(nodes)

        # ★追加: Software Center のヘッダー（タブ）領域を事前検出
        # これがあれば、その下の領域を強制的に CONTENT にする
        sw_center_content_bbox = None
        tab_keywords = {"Explore", "Installed", "Updates"}
        sw_tabs = [n for n in nodes if (n.get("tag") == "radio-button" and n.get("name") in tab_keywords)]
        
        if len(sw_tabs) >= 2:
            # タブの座標から、ウィンドウとおぼしき領域（特に下方向）を定義
            txs = [node_bbox_from_raw(n)["x"] for n in sw_tabs]
            tys = [node_bbox_from_raw(n)["y"] for n in sw_tabs]
            
            # 左右に大きく広げて、左端のSearchボタンや、右端のアプリ列もカバーする
            # タブのY座標より下はすべてコンテンツとみなす
            sw_center_content_bbox = {
                "min_x": min(txs) - 500, # 左側のSearchボタンやアプリ列を含めるため広くとる
                "max_x": max(txs) + 400, # 右側のアプリ列を含める
                "min_y": min(tys) - 50,  # タブの少し上から
                "max_y": h               # 画面下まで
            }
        else:
            # ★修正: 詳細画面 (Details View) の検出強化
            # "Source" ボタン、または "Go back" ボタンを探す
            header_anchor = next((n for n in nodes if n.get("name") == "Source" and n.get("tag") in {"menu-button", "combo-box", "push-button", "label"}), None)
            if not header_anchor:
                header_anchor = next((n for n in nodes if n.get("name") in {"Go back", "Back"} and n.get("tag") in {"push-button", "icon"}), None)
            
            if header_anchor:
                sb = node_bbox_from_raw(header_anchor)
                if sb["y"] < 200:
                    sw_center_content_bbox = {
                        "min_x": 0,
                        "max_x": w,
                        "min_y": sb["y"] - 30, # 少し余裕を持つ
                        "max_y": h
                    }

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            cx, cy = bbox_to_center_tuple(bbox)

            tag  = (n.get("tag") or "").lower()
            role = (n.get("role") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            name_lower = name.lower()

            # 1. APP_LAUNCHER
            if (
                x < LAUNCHER_X_LIMIT
                and bh > 32
                and bw < w * 0.12
                and tag in {"push-button", "toggle-button", "launcher-app"}
                and name
            ):
                regions["APP_LAUNCHER"].append(n)
                continue

            # 2. TOP_BAR
            if cy < TOP_BAR_MAX_Y:
                if tag in {
                    "label", "push-button", "toggle-button", "menu",
                    "image", "icon", "text",
                }:
                    regions["TOP_BAR"].append(n)
                    continue

            # 3. MODAL
            if role in {"dialog", "alert"} or any(
                kw in name_lower for kw in getattr(self, "MODAL_KEYWORDS", [])
            ):
                regions["MODAL"].append(n)
                continue

            # 4. 強制CONTENT領域 (ウィンドウBBox優先ルール)
            is_forced_content = False
            
            # A. サイドバー
            if sidebar_bbox:
                if (sidebar_bbox["x"] <= cx <= sidebar_bbox["x"] + sidebar_bbox["w"]) and (sidebar_bbox["y"] <= cy <= sidebar_bbox["y"] + sidebar_bbox["h"]):
                    is_forced_content = True
            
            # B. パンくずリスト
            if not is_forced_content and breadcrumb_bbox:
                if (breadcrumb_bbox["x"] <= cx <= breadcrumb_bbox["x"] + breadcrumb_bbox["w"]) and (breadcrumb_bbox["y"] <= cy <= breadcrumb_bbox["y"] + breadcrumb_bbox["h"]):
                    is_forced_content = True
            
            # C. ウィンドウ領域 (通常)
            if not is_forced_content and window_regions:
                for wb in window_regions:
                    if (wb["x"] - 10 <= cx <= wb["x"] + wb["w"] + 10) and \
                       (wb["y"] - 10 <= cy <= wb["y"] + wb["h"] + 10):
                        is_forced_content = True
                        break
            
            # D. ★追加: Software Center 領域
            if not is_forced_content and sw_center_content_bbox:
                if (sw_center_content_bbox["min_x"] <= cx <= sw_center_content_bbox["max_x"]) and \
                   (cy >= sw_center_content_bbox["min_y"]):
                    is_forced_content = True

            if is_forced_content:
                regions["CONTENT"].append(n)
                continue

            # 5. OS_POPUP
            if (
                bw < w * 0.35
                and bh < h * 0.25
                and (
                    tag in {"entry", "text", "textbox"}
                    or "rename" in name_lower
                    or "folder name" in name_lower
                )
            ):
                regions["OS_POPUP"].append(n)
                continue

            # 6. DESKTOP_ICONS
            if (
                tag in {"label", "icon", "image", "push-button"}
                and name
            ):
                lower = name_lower
                if (
                    "minimize" in lower
                    or "maximize" in lower
                    or "close" in lower
                    or lower.startswith(("user@", "root@", "~"))
                    or lower.endswith("$")
                    or lower.endswith("#")
                    or "@" in lower and ":" in lower
                    or "/home/" in lower
                    or "terminal" in lower
                    or tag == "menu"
                    or name == "/"
                    or "software updates" in lower
                ):
                    regions["CONTENT"].append(n)
                    continue

                regions["DESKTOP_ICONS"].append(n)
                continue

            # 7. フォールバック
            regions["CONTENT"].append(n)

        return regions

    # === 圧縮系ユーティリティ (共通) ===

    def _format_center(self, n: Node) -> str:
        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        return f"@ ({cx}, {cy})"

    # === OS向け 各領域の圧縮ロジック ===

    def _compress_app_launcher(self, nodes: List[Node]) -> List[str]:
        """
        左ドックのアプリランチャー。
        - y座標でソート
        - アプリ名でdedup
        """
        lines: List[str] = []

        # y座標順に並べる
        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[1]
        )

        seen_names: set[str] = set()
        for n in sorted_nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            if name in seen_names:
                continue
            seen_names.add(name)

            center_str = self._format_center(n)
            lines.append(f"[launcher-app] \"{name}\" {center_str}")

        return lines

    def _compress_top_bar(self, nodes: List[Node]) -> List[str]:
        """
        画面最上部のバー (Activities / 日付時刻 / ネットワーク / 電源 など)。
        あまり細かく出しすぎるとノイズなので、ざっくり種別ごとにまとめる。
        """
        lines: List[str] = []

        # 左→右に並べたいので x でソート
        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[0]
        )

        seen: set[str] = set()

        for n in sorted_nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            lower = name.lower()
            center_str = self._format_center(n)

            # 種別ごとの簡単な分類
            if "activities" in lower:
                prefix = "[top-activities]"
            elif any(k in lower for k in ("am", "pm", ":")) and any(c.isdigit() for c in name):
                # かなり雑な時計判定
                prefix = "[top-clock]"
            elif any(k in lower for k in ("wifi", "wireless", "network")):
                prefix = "[top-network]"
            elif "battery" in lower:
                prefix = "[top-battery]"
            elif any(k in lower for k in ("sound", "volume", "speaker")):
                prefix = "[top-sound]"
            else:
                prefix = "[top-item]"

            key = f"{prefix}:{name}"
            if key in seen:
                continue
            seen.add(key)

            lines.append(f"{prefix} \"{name}\" {center_str}")

        return lines

    def _compress_desktop_icons(self, nodes: List[Node]) -> List[str]:
        """
        デスクトップ上のアイコン (Home フォルダなど)。
        - y, x でソート
        - シンプルに [desktop-icon] として出す
        """
        lines: List[str] = []

        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[1:]  # (y, x)
        )

        seen_names: set[str] = set()

        for n in sorted_nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            if name in seen_names:
                continue
            seen_names.add(name)

            center_str = self._format_center(n)
            lines.append(f"[desktop-icon] \"{name}\" {center_str}")

        return lines

    def _compress_os_popup(self, nodes: List[Node]) -> List[str]:
        """
        デスクトップ上の小さなポップアップ (Rename ダイアログなど)。
        - entry / textbox は [os-popup-entry]
        - ボタンは [os-popup-btn]
        - それ以外は [os-popup-item]
        同じポップアップ内の部品が入っている前提なので、そのまま列挙で十分。
        """
        lines: List[str] = []

        for n in nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            tag = (n.get("tag") or "").lower()
            center_str = self._format_center(n)

            if tag in {"entry", "textbox", "text"}:
                prefix = "[os-popup-entry]"
            elif tag in {"push-button", "toggle-button"}:
                prefix = "[os-popup-btn]"
            else:
                prefix = "[os-popup-item]"

            lines.append(f"{prefix} \"{name}\" {center_str}")

        return lines

    def _compress_modal(self, nodes: List[Node], w: int, h: int) -> List[str]:
        """
        OS 全体にかぶさるモーダルダイアログ。
        既存の process_region_lines を流用。
        """
        return self.process_region_lines(nodes, w, h)

    # === ★新規: ウィンドウ検出 & コンテンツ/モーダル振り分けロジック ===

    def _detect_and_classify_nodes(self, all_nodes: List[Node]) -> Tuple[List[str], List[Node]]:
        """
        全ノードを対象にウィンドウ検知を行う。
        ★修正: 本物のモーダル（暗転あり・キーワード一致）を最優先で検出し、ウィンドウへの吸収を防ぐ。
        """
        if not all_nodes: return [], []
        
        remaining_nodes = list(all_nodes)
        detected_windows = []
        true_modal_nodes = [] # 確定モーダルリスト
        used_ids = set()

        # --- 0. ★新規: 明らかなモーダルを先行抽出 ---
        # 暗転レイヤー検出
        bboxes = [node_bbox_from_raw(n) for n in remaining_nodes]
        screen_area = 1920 * 1080 # 仮
        dim_layer = None
        
        for n in remaining_nodes:
            b = node_bbox_from_raw(n)
            # 画面の30%以上覆うテキスト無しのパネル
            if (b["w"]*b["h"])/screen_area > 0.3 and len((n.get("text") or n.get("name") or "").strip()) < 3:
                tag = (n.get("tag") or "").lower()
                if tag in {"panel", "frame", "image", "static", "text"}:
                    dim_layer = n
                    break
        
        if dim_layer:
            dim_box = node_bbox_from_raw(dim_layer)
            # 暗転レイヤーの上に乗っている（z-orderは不明だが包含関係やキーワードで推測）要素を探す
            # 特に「Authentication」「Password」などのキーワードを持つ小さな領域
            potential_modal_nodes = []
            has_modal_keyword = False
            
            for n in remaining_nodes:
                if n == dim_layer: continue
                # 暗転レイヤーの範囲内にあるか？（全画面暗転なら全部入るが...）
                
                # キーワードチェック
                name = (n.get("name") or n.get("text") or "").strip().lower()
                if any(k in name for k in self.MODAL_KEYWORDS):
                    has_modal_keyword = True
                
                # モーダル構成要素っぽいタグ
                if n.get("role") == "dialog" or n.get("tag") in {"push-button", "entry", "password-text", "label"}:
                     # ここでは簡易的に、暗転レイヤーが存在するなら、
                     # その上で「中央付近」に集まっている小さなグループをモーダルとする判定などが理想だが、
                     # まずはキーワードにヒットするグループを隔離する
                     pass

            # 簡易実装: 暗転レイヤーがあり、かつ "Authentication" 等の強いキーワードがあれば、
            # そのキーワードを持つノードと、その周囲(近傍)のノードをモーダルとして確定させる
            
            # 中心点付近のクラスタリング
            if has_modal_keyword:
                # 強いキーワードを持つノードを探す
                triggers = [n for n in remaining_nodes if any(k in (n.get("name") or "").lower() for k in self.MODAL_KEYWORDS)]
                if triggers:
                    # 最初のトリガーを中心に、一定距離内のノードをモーダルとして回収
                    ref_box = node_bbox_from_raw(triggers[0])
                    ref_cx, ref_cy = ref_box["x"]+ref_box["w"]/2, ref_box["y"]+ref_box["h"]/2
                    
                    for n in remaining_nodes:
                        nb = node_bbox_from_raw(n)
                        ncx, ncy = nb["x"]+nb["w"]/2, nb["y"]+nb["h"]/2
                        # 距離500px以内ならモーダルの仲間とみなす
                        if abs(ncx - ref_cx) < 500 and abs(ncy - ref_cy) < 400:
                            true_modal_nodes.append(n)
                            used_ids.add(id(n))
                    
                    # 暗転レイヤー自体は背景扱いでもいいが、モーダルの一部としてもよい
                    # ここでは除外済み扱いにする
                    used_ids.add(id(dim_layer))

        # --- 1. サイドバー領域検出 (補正用) ---
        sidebar_bbox = self._detect_sidebar_region(all_nodes)

        # --- 2. Closeボタンを持つウィンドウ検出 ---
        close_buttons = [n for n in remaining_nodes if (n.get("tag") in {"push-button", "toggle-button"} and n.get("name") == "Close")]
        
        for close_btn in close_buttons:
            if id(close_btn) in used_ids: continue
            c_box = node_bbox_from_raw(close_btn)
            c_cy, c_right = c_box["y"] + c_box["h"]/2, c_box["x"] + c_box["w"]
            
            best_title, min_dist = None, float("inf")
            for n in remaining_nodes:
                if n == close_btn: continue
                n_box = node_bbox_from_raw(n)
                if abs((n_box["y"]+n_box["h"]/2) - c_cy) > 20 or n_box["x"] >= c_box["x"]: continue
                tag, name = (n.get("tag") or "").lower(), (n.get("name") or n.get("text") or "").strip()
                if tag == "label" and name:
                    dist = c_box["x"] - (n_box["x"] + n_box["w"])
                    if dist < min_dist: min_dist, best_title = dist, n

            header_nodes = [close_btn]
            if best_title: header_nodes.append(best_title)
            
            # 周辺ボタン回収
            for n in remaining_nodes:
                if id(n) in used_ids or n in header_nodes: continue
                n_box = node_bbox_from_raw(n)
                if abs((n_box["y"]+n_box["h"]/2) - c_cy) <= 15 and n_box["x"] < c_right and n_box["x"] > (c_box["x"] - 600):
                    if (n.get("tag") or "").lower() in {"push-button", "toggle-button", "label", "icon"}:
                        header_nodes.append(n)

            min_x = min(node_bbox_from_raw(n)["x"] for n in header_nodes)
            
            # サイドバーによる左端拡張
            if sidebar_bbox:
                if abs(c_box["y"] - sidebar_bbox["y"]) < 100 and min_x > sidebar_bbox["x"]:
                     min_x = sidebar_bbox["x"]

            # ターミナルBody早期回収
            content_nodes = []
            terminals = [n for n in remaining_nodes if (n.get("tag") or "").lower() == "terminal" and id(n) not in used_ids]
            for term in terminals:
                t_box = node_bbox_from_raw(term)
                if t_box["y"] >= c_box["y"] and t_box["y"] < (c_box["y"] + 200) and t_box["x"] < c_right and (t_box["x"] + t_box["w"]) > min_x:
                     content_nodes.append(term); used_ids.add(id(term))

            for hn in header_nodes: used_ids.add(id(hn))
            title_text = (best_title.get("name") or "").strip() if best_title else "Unknown Window"
            
            detected_windows.append({
                "title": title_text,
                "limit_max_x": c_right, "limit_min_x": min_x - 30,
                "header_y": c_box["y"], "header_bottom": c_box["y"] + c_box["h"],
                "header_nodes": header_nodes, "content_nodes": content_nodes
            })

        # --- 3. Menuボタンx3 パターン ---
        menu_buttons = [n for n in remaining_nodes if (n.get("tag") in {"push-button", "toggle-button"} and n.get("name") == "Menu") and id(n) not in used_ids]
        menu_rows, processed_menus = [], set()
        for btn in menu_buttons:
            if id(btn) in processed_menus: continue
            row = [btn]; processed_menus.add(id(btn))
            base_cy = node_bbox_from_raw(btn)["y"] + node_bbox_from_raw(btn)["h"]/2
            for other in menu_buttons:
                if id(other) in processed_menus: continue
                if abs((node_bbox_from_raw(other)["y"]+node_bbox_from_raw(other)["h"]/2) - base_cy) < 10:
                    row.append(other); processed_menus.add(id(other))
            menu_rows.append(row)

        for row in menu_rows:
            if len(row) >= 3:
                row.sort(key=lambda n: node_bbox_from_raw(n)["x"])
                rightmost = row[-1]
                r_box = node_bbox_from_raw(rightmost)
                limit_max = r_box["x"] + r_box["w"]
                
                l_cy = node_bbox_from_raw(row[0])["y"] + node_bbox_from_raw(row[0])["h"]/2
                l_x = node_bbox_from_raw(row[0])["x"]
                best_title, min_dist = None, float("inf")
                for n in remaining_nodes:
                    if id(n) in used_ids or n in row: continue
                    n_box = node_bbox_from_raw(n)
                    if abs((n_box["y"]+n_box["h"]/2) - l_cy) > 15 or n_box["x"] >= l_x: continue
                    tag, name = (n.get("tag") or "").lower(), (n.get("name") or n.get("text") or "").strip()
                    if tag == "label" and name:
                        dist = l_x - (n_box["x"] + n_box["w"])
                        if dist < min_dist: min_dist, best_title = dist, n
                
                header_nodes = row
                if best_title: header_nodes = [best_title] + header_nodes
                min_x = min(node_bbox_from_raw(n)["x"] for n in header_nodes)
                
                if sidebar_bbox and abs(r_box["y"] - sidebar_bbox["y"]) < 100 and min_x > sidebar_bbox["x"]:
                     min_x = sidebar_bbox["x"]

                for hn in header_nodes: used_ids.add(id(hn))
                detected_windows.append({
                    "title": (best_title.get("name") or "").strip() if best_title else "Unknown Window (Menu)",
                    "limit_max_x": limit_max, "limit_min_x": min_x - 30,
                    "header_y": r_box["y"], "header_bottom": r_box["y"] + r_box["h"],
                    "header_nodes": header_nodes, "content_nodes": []
                })
        # --- 3.5 Software Center (Tabbed Interface) ---
        # ★新規追加: "Explore", "Installed", "Updates" のラジオボタン列をヘッダーとする
        tab_keywords = {"Explore", "Installed", "Updates"}
        sw_tabs = [n for n in remaining_nodes if (n.get("tag") == "radio-button" and n.get("name") in tab_keywords) and id(n) not in used_ids]
        
        if len(sw_tabs) >= 2:
             sw_tabs.sort(key=lambda n: node_bbox_from_raw(n)["x"])
             header_nodes = list(sw_tabs)
             min_x = node_bbox_from_raw(sw_tabs[0])["x"]
             max_x = node_bbox_from_raw(sw_tabs[-1])["x"] + node_bbox_from_raw(sw_tabs[-1])["w"]
             header_y = node_bbox_from_raw(sw_tabs[0])["y"]
             header_bottom = header_y + node_bbox_from_raw(sw_tabs[0])["h"]
             
             # 左側にあるSearchボタン(toggle-button)もヘッダーに含める
             for n in remaining_nodes:
                 if id(n) in used_ids: continue
                 if n.get("name") == "Search":
                     b = node_bbox_from_raw(n)
                     if abs(b["y"] - header_y) < 20 and b["x"] < min_x:
                         header_nodes.append(n)
                         min_x = min(min_x, b["x"])
             
             for hn in header_nodes: used_ids.add(id(hn))
             
             detected_windows.append({
                "title": "Ubuntu Software",
                "limit_max_x": max_x + 100, "limit_min_x": min_x - 50,
                "header_y": header_y, "header_bottom": header_bottom,
                "header_nodes": header_nodes, "content_nodes": []
            })

        # --- 3.6 Software Center (Details Interface) ---
        # ★追加: 詳細画面の検出ロジック
        # "Source" ボタン、または "Go back" ボタンを探してヘッダーの基準にする
        header_anchor = next((n for n in remaining_nodes if n.get("name") == "Source" and id(n) not in used_ids), None)
        
        if not header_anchor:
             header_anchor = next((n for n in remaining_nodes if n.get("name") in {"Go back", "Back"} and id(n) not in used_ids), None)

        if header_anchor:
             sb = node_bbox_from_raw(header_anchor)
             # 画面上部にあるかチェック
             if sb["y"] < 200:
                 header_nodes = [header_anchor]
                 header_y = sb["y"]
                 header_bottom = header_y + sb["h"]
                 
                 # 同じ高さにある他のヘッダー要素（検索アイコンなど）を回収
                 for n in remaining_nodes:
                     if id(n) in used_ids or n in header_nodes: continue
                     nb = node_bbox_from_raw(n)
                     if abs(nb["y"] - header_y) < 20:
                         header_nodes.append(n)

                 for hn in header_nodes: used_ids.add(id(hn))
                 
                 # 画面幅いっぱいのウィンドウとして定義
                 detected_windows.append({
                    "title": "Ubuntu Software",
                    "limit_max_x": 3000, 
                    "limit_min_x": 0,
                    "header_y": header_y, 
                    "header_bottom": header_bottom,
                    "header_nodes": header_nodes, 
                    "content_nodes": []
                })

        # --- 4. ターミナルフォールバック ---
        terminals = [n for n in remaining_nodes if (n.get("tag") or "").lower() == "terminal" and id(n) not in used_ids]
        for term in terminals:
            t_box = node_bbox_from_raw(term)
            is_covered = False
            for w in detected_windows:
                if id(term) in [id(c) for c in w["content_nodes"]]: is_covered = True
            if is_covered: continue
            detected_windows.append({
                "title": "Terminal", "limit_max_x": t_box["x"] + t_box["w"], "limit_min_x": t_box["x"],
                "header_y": t_box["y"] - 40, "header_bottom": t_box["y"],
                "header_nodes": [], "content_nodes": [term]
            })
            used_ids.add(id(term))

        # --- 5. ノード振り分け (垂直距離優先) ---
        orphans = []
        for n in remaining_nodes:
            if id(n) in used_ids: continue
            n_box = node_bbox_from_raw(n)
            n_cx, n_cy = n_box["x"] + n_box["w"]/2, n_box["y"] + n_box["h"]/2
            
            best_window, min_score = None, float("inf")
            for w in detected_windows:
                if n_cx > w["limit_max_x"] + 50: continue
                if n_cx < w["limit_min_x"] - 50: continue
                dy = n_cy - w["header_bottom"]
                if dy < -10: continue 
                
                horizontal_pen = 0
                if n_cx < w["limit_min_x"]: horizontal_pen = w["limit_min_x"] - n_cx
                elif n_cx > w["limit_max_x"]: horizontal_pen = n_cx - w["limit_max_x"]
                score = dy + (horizontal_pen * 5.0)
                if score < min_score: min_score, best_window = score, w
            
            if best_window: best_window["content_nodes"].append(n)
            else: orphans.append(n)

        # --- 6. Orphans を モーダル判定 ---
        # ウィンドウに属さなかったノードだけが、真のモーダルの可能性がある
        true_modal_nodes = []
        background_orphans = []
        
        # 簡易判定: 暗転レイヤー or 明確なダイアログ構造
        # (ここでは簡易的に、以前の_filter_modal_nodesのロジックの一部を適用)
        if orphans:
             # 暗転レイヤー検出
             bboxes = [node_bbox_from_raw(n) for n in orphans]
             has_dim = False
             screen_area = 1920 * 1080 # 仮
             if bboxes:
                 min_x, min_y = min(b["x"] for b in bboxes), min(b["y"] for b in bboxes)
                 max_x, max_y = max(b["x"]+b["w"] for b in bboxes), max(b["y"]+b["h"] for b in bboxes)
                 union_area = (max_x - min_x) * (max_y - min_y)
                 
                 for n in orphans:
                     b = node_bbox_from_raw(n)
                     if (b["w"]*b["h"]) > 500000 and len((n.get("text") or "").strip()) < 3: # 大まかな判定
                          has_dim = True

                 # 判定: 暗転がある、もしくはボタン+ラベルの小規模な集合ならモーダル
                 # Settingsの変更などは「ラベルだけ」なので、ここには来ない(ウィンドウに吸収済み)
                 # 万が一吸収されなかったラベル群は、CONTENT(Background)に落とすのが安全
                 
                 # ここでは「明らかにモーダル」な条件を厳しめにする
                 is_likely_modal = has_dim or any((n.get("role")=="dialog") for n in orphans)
                 
                 if is_likely_modal:
                     true_modal_nodes = orphans
                 else:
                     background_orphans = orphans

        # --- 7. 出力テキスト生成 ---
        content_lines = []
        detected_windows.sort(key=lambda w: (w["header_y"], w["limit_min_x"]))
        for w in detected_windows:
            all_nodes = w["header_nodes"] + w["content_nodes"]
            if not all_nodes: continue
            content_lines.append(f"=== Window: {w['title']} ===")
            content_lines.extend(self._format_node_list(all_nodes))
            content_lines.append("")
        
        if background_orphans:
            if detected_windows: content_lines.append("=== Background / Other ===")
            content_lines.extend(self._format_node_list(background_orphans))

        return content_lines, true_modal_nodes



    def _format_node_list(self, nodes: List[Node]) -> List[str]:
        """
        ノードリストをフォーマットして文字列リストにするヘルパー
        """
        lines = []
        # (y, x) 順にソート
        sorted_nodes = sorted(
            nodes,
            key=lambda n: bbox_to_center_tuple(node_bbox_from_raw(n))[1:]
        )
        
        seen_keys = set()

        for n in sorted_nodes:
            name = (n.get("name") or n.get("text") or "").strip()
            # ターミナルの場合、内容が空でも存在を示す
            tag = (n.get("tag") or "").lower()
            
            if not name and tag != "terminal":
                continue

            center_str = self._format_center(n)
            
            # 重複排除
            dedup_key = f"{name}|{center_str}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            # Prefix決定
            if tag == "terminal":
                summary = (n.get("text") or "").strip() or name or "(terminal)"
                lines.append(f"[terminal] \"{summary}\" {center_str}")
                continue
            
            if tag in {"push-button", "toggle-button"}:
                prefix = "[btn]"
            elif tag in {"check-box", "radio-button"}:
                prefix = "[check]"
            elif tag in {"entry", "text", "password-text"}:
                prefix = "[input]"
            elif tag in {"combo-box", "menu-button"}:
                prefix = "[combo]"
            elif tag in {"spin-button", "slider", "scroll-bar"}:
                prefix = "[control]"
            else:
                prefix = "[text]"

            lines.append(f"{prefix} \"{name}\" {center_str}")
            
        return lines


    def _filter_modal_nodes(
        self,
        modal_nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> List[Node]:
        """
        ハイブリッド判定: 暗転レイヤー + ボタン構造 + タグ + 画面占有率
        これによりターミナル等の新規ウィンドウはモーダルから除外される。
        """
        if not modal_nodes: return []
        
        # 1. 許可リスト除外 (Terminalなどはウィンドウ扱い)
        IGNORE_TAGS = {"terminal", "application", "window", "frame"}
        IGNORE_ROLES = {"application", "window", "frame"}
        for n in modal_nodes:
            if (n.get("tag") or "").lower() in IGNORE_TAGS or (n.get("role") or "").lower() in IGNORE_ROLES:
                return []
        
        # 2. ボタン構造判定 (Minimize/Maximizeがあれば通常ウィンドウ扱い)
        WINDOW_CTRL_NAMES = {"minimize", "maximize"}
        for n in modal_nodes:
            name = (n.get("name") or "").strip().lower()
            if name in WINDOW_CTRL_NAMES:
                return []

        # 3. 暗転レイヤー検出 (画面の30%以上を覆うテキスト無しのパネル)
        screen_area = max(screen_w * screen_h, 1)
        has_dim = False
        bboxes = [node_bbox_from_raw(n) for n in modal_nodes]
        
        min_x = min(b["x"] for b in bboxes)
        min_y = min(b["y"] for b in bboxes)
        max_x = max(b["x"]+b["w"] for b in bboxes)
        max_y = max(b["y"]+b["h"] for b in bboxes)
        union_area = (max_x - min_x) * (max_y - min_y)
        
        for n in modal_nodes:
            b = node_bbox_from_raw(n)
            if (b["w"]*b["h"])/screen_area > 0.3 and len((n.get("text") or n.get("name") or "").strip()) < 3:
                tag = (n.get("tag") or "").lower()
                if tag in {"panel", "frame", "image", "static", "text"}:
                    has_dim = True; break
        
        # 4. 判定
        # 画面の40%以上を占める「大物」で、かつ暗転レイヤーがない場合は、ただの新規ウィンドウとみなす
        if (union_area / screen_area) > 0.4 and not has_dim:
            return []
            
        return modal_nodes

        


    # === メイン出力 ===
    def _build_output(
        self,
        regions: Dict[str, List[Node]],
        modal_nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> List[str]:
        lines = []

        if r := self._compress_app_launcher(regions.get("APP_LAUNCHER", [])): lines.append("APP_LAUNCHER:"); lines.extend(r)
        if r := self._compress_top_bar(regions.get("TOP_BAR", [])): lines.append("TOP_BAR:"); lines.extend(r)
        if r := self._compress_desktop_icons(regions.get("DESKTOP_ICONS", [])): lines.append("DESKTOP_ICONS:"); lines.extend(r)
        if r := self._compress_os_popup(regions.get("OS_POPUP", [])): lines.append("OS_POPUP:"); lines.extend(r)

        # ★統合処理: CONTENTとDiffモーダルを混ぜて、ウィンドウ検出で再分類
        all_dynamic_nodes = regions.get("CONTENT", []) + (modal_nodes or [])
        # 重複ID排除 (念のため)
        unique_nodes = {id(n): n for n in all_dynamic_nodes}.values()
        
        content_lines, true_modals = self._detect_and_classify_nodes(list(unique_nodes))
        
        if content_lines:
            lines.append("CONTENT:")
            lines.extend(content_lines)
            
        if true_modals:
             lines.append("MODAL:")
             lines.extend(self._format_node_list(true_modals))

        return lines
