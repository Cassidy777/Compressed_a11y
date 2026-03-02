import re
from typing import List, Dict, Tuple, Set, Optional, Any

from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    dedup_horizontal_menu_nodes,
)

class VlcCompressor(BaseA11yCompressor):
    domain_name = "vlc"

    # 背景削除は誤判定のリスクがあるためFalse、ステータスバーは情報源として使う
    enable_background_filtering = False
    use_statusbar = True

    # VLC menubar (menu-item)
    MENU_KEYWORDS: Set[str] = {
        "media", "playback", "audio", "video", "subtitle", "tools", "view", "help",
    }

    # 将来「本物のダイアログ」を keyword で拾いたい時に足す
    MODAL_KEYWORDS: Set[str] = {
        # "preferences", "open media", "error", "codec", ...
    }

    def __init__(self):
        super().__init__()
        # Preferences 継続判定用の状態
        self._vlc_pref_active: bool = False

        # 「前回のPreferences要素」を保存（tag + 中心座標）
        # 次ステップでは modal_nodes からこれに近いもの(±20px)をPreferencesとして復元する
        self._vlc_pref_prev: List[Tuple[str, int, int]] = []  # (tag, cx, cy)



    # ----------------------------
    # 基本：static/dynamic splitしない
    # ----------------------------
    def split_static_ui(self, nodes: List[Node], w: int, h: int):
        # VLCは static/dynamic 分離しない（diff の prev_base を小さくしない）
        return nodes, []


    # ----------------------------
    # セマンティック領域分割
    # ----------------------------
    def get_semantic_regions(
        self, nodes: List[Node], w: int, h: int, dry_run: bool = False
    ) -> Dict[str, List[Node]]:

        if not getattr(self, "enable_region_segmentation", True):
            return {"CONTENT": nodes}

        regions: Dict[str, List[Node]] = {
            "MENUBAR": [],
            "APP_LAUNCHER": [],
            "TOP_BAR": [],     # window/system bar etc.
            "CONTENT": [],
            "STATUSBAR": [],
            "MODAL": [],       # keyword-based modal (optional)
        }

        LAUNCHER_X_LIMIT = w * 0.06          # 左端ランチャー
        MENUBAR_MAX_Y    = h * 0.10          # 上部メニューの高さ
        TOP_BAR_MAX_Y    = h * 0.20          # 上20%をバー領域扱い
        STATUS_MIN_Y     = h * 0.92          # 最下部

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or n.get("description") or "").strip()
            name_lower = name.lower()

            # 1) APP_LAUNCHER
            if x < LAUNCHER_X_LIMIT and bw < w * 0.08 and bh > 30:
                if tag in ("push-button", "toggle-button", "launcher-app"):
                    regions["APP_LAUNCHER"].append(n)
                    continue

            # 2) MODAL (keyword based; 今は空でもOK)
            if self.MODAL_KEYWORDS and any(kw in name_lower for kw in self.MODAL_KEYWORDS):
                regions["MODAL"].append(n)
                continue

            # 3) STATUSBAR（最下部）
            if cy > STATUS_MIN_Y:
                # VLCは bottom に時間表示が label で出る
                if tag in ("statusbar", "status", "label"):
                    # Unityの "Home" みたいなゴミを落とす
                    if name_lower == "home":
                        continue
                    regions["STATUSBAR"].append(n)
                    continue

            # 4) 上部バー（MENUBAR / TOP_BAR）
            if cy < TOP_BAR_MAX_Y:
                # 4-1) MENUBAR: VLCは menu-item が並ぶ
                if cy < MENUBAR_MAX_Y and tag == "menu-item":
                    if name_lower in self.MENU_KEYWORDS:
                        regions["MENUBAR"].append(n)
                        continue

                # 4-2) TOP_BAR: window title / system menu など
                if tag in ("menu", "push-button", "toggle-button", "label", "text"):
                    regions["TOP_BAR"].append(n)
                    continue

            # 5) それ以外は CONTENT
            regions["CONTENT"].append(n)

        return regions

    # # ----------------------------
    # # 表示用フォーマット
    # # ----------------------------
    # def _format_center(self, n: Node) -> str:
    #     bbox = node_bbox_from_raw(n)
    #     cx, cy = bbox_to_center_tuple(bbox)
    #     return f"@ ({cx}, {cy})"

    # -------------------------------------------------------------------------
    # 座標と追加属性のフォーマット
    # -------------------------------------------------------------------------
    def _format_center(self, node: Node) -> str:
        """フラグに応じて、中心座標(圧縮ON)か、生BBox+詳細属性(圧縮OFF)を切り替えて返す"""
        bbox = node_bbox_from_raw(node)
        
        # engine.pyで追加したフラグをチェック
        if getattr(self, "enable_redundancy_reduction", True):
            # ==========================================
            # ★ Trueの時: 圧縮・冗長性削減 ON (元の挙動)
            # ==========================================
            cx, cy = bbox_to_center_tuple(bbox)
            return f"@ ({cx}, {cy})"
        else:
            # ==========================================
            # ★ Falseの時: 圧縮・冗長性削減 OFF (生のデータ)
            # ==========================================
            desc = (node.get("description") or "").strip()
            desc_attr = f' desc="{desc}"' if desc else ""
            
            role = (node.get("role") or "").strip()
            role_attr = f' role="{role}"' if role else ""
            
            # 属性文字列と生のバウンディングボックス(x, y, w, h)を結合して返す
            return f"{desc_attr}{role_attr} @ ({bbox['x']}, {bbox['y']}, {bbox['w']}, {bbox['h']})"


    # ----------------------------
    # Helpers (Preferences / FileChooser split)
    # ----------------------------
    @staticmethod
    def _disp(n: Node) -> str:
        return (n.get("name") or n.get("text") or "").strip().lower()

    @staticmethod
    def _center_xy(n: Node) -> Tuple[int, int]:
        cx, cy = bbox_to_center_tuple(node_bbox_from_raw(n))
        return int(cx), int(cy)

    @staticmethod
    def _is_pref_candidate_tag(tag: str) -> bool:
        # Preferences本体で意味があるもの（必要なら増やす）
        return tag in {
            "label", "check-box", "combo-box", "spin-button", "push-button", "entry", "text"
        }

    def _update_pref_prev(self, pref_nodes: List[Node]) -> None:
        prev: List[Tuple[str, int, int]] = []
        for n in pref_nodes:
            tag = (n.get("tag") or "").lower()
            if not self._is_pref_candidate_tag(tag):
                continue
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            # Home / --:-- はノイズになりがち
            if tag == "label" and name.strip().lower() in {"home", "--:--"}:
                continue
            cx, cy = self._center_xy(n)
            prev.append((tag, cx, cy))
        self._vlc_pref_prev = prev

    def _split_pref_by_prev(self, modal_nodes: List[Node], tol_px: int = 20) -> Tuple[List[Node], List[Node]]:
        """
        modal_nodes の中から「前回Preferences座標に近いもの」をPreferencesとして回収し、
        残りをMODALとして返す。
        """
        if not self._vlc_pref_prev:
            return [], list(modal_nodes or [])

        pref_nodes: List[Node] = []
        rest_nodes: List[Node] = []

        prev = self._vlc_pref_prev

        for n in modal_nodes or []:
            tag = (n.get("tag") or "").lower()
            if not self._is_pref_candidate_tag(tag):
                rest_nodes.append(n)
                continue

            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                rest_nodes.append(n)
                continue

            if tag == "label" and name.lower() in {"home", "--:--"}:
                rest_nodes.append(n)
                continue

            cx, cy = self._center_xy(n)

            # tag が同じで、中心座標が近い（±tol）なら Preferences とみなす
            matched = False
            for ptag, px, py in prev:
                if ptag != tag:
                    continue
                if abs(cx - px) <= tol_px and abs(cy - py) <= tol_px:
                    matched = True
                    break

            if matched:
                pref_nodes.append(n)
            else:
                rest_nodes.append(n)

        return pref_nodes, rest_nodes

    @staticmethod
    def _filechooser_markers() -> Set[str]:
        return {
            "look in:",
            "files of type:",
            "parent directory",
            "create new folder",
            "list view",
            "detail view",
            "choose",
            "directories",
            "table-cell",
        }

    def _looks_like_filechooser(self, nodes: List[Node]) -> bool:
        texts = [self._disp(n) for n in nodes if self._disp(n)]
        hits = sum(1 for t in texts if t in self._filechooser_markers())
        return hits >= 2

    def _is_filechooser_node(self, n: Node, w: int, h: int, filechooser_present: bool) -> bool:
        """
        filechooser が出てるときだけ使う「ノード単位の振り分け」。
        markers一致を最優先、次に table/list の領域っぽいものを拾う。
        """
        if not filechooser_present:
            return False

        tag = (n.get("tag") or "").lower()
        t = self._disp(n)

        # 1) marker一致は即 chooser
        if t in self._filechooser_markers():
            return True

        b = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(b)

        # 2) chooser の“典型領域”にいる要素をまとめて chooser 扱い
        in_chooser_panel = (w * 0.30) <= cx <= (w * 0.78) and (h * 0.20) <= cy <= (h * 0.82)
        if in_chooser_panel and tag in {"table-cell", "list-item", "combo-box", "push-button", "label", "text"}:
            return True

        return False


    # ----------------------------
    # 圧縮：MENUBAR
    # ----------------------------
    def _compress_menubar(self, nodes: List[Node]) -> List[str]:
        lines: List[str] = []
        # menu-item でも dedup が効くように共通関数を流用（合わなければ後で差し替え）
        deduped = dedup_horizontal_menu_nodes(nodes)

        seen = set()
        for n in deduped:
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            lower = name.lower()
            if lower not in self.MENU_KEYWORDS:
                continue
            if lower in seen:
                continue
            seen.add(lower)
            lines.append(f"[menu-item] \"{name}\" {self._format_center(n)}")
        return lines

    # ----------------------------
    # 圧縮：TOP_BAR（SYSTEM）
    # ----------------------------
    def _compress_top_bar(self, nodes: List[Node]) -> List[str]:
        lines: List[str] = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            # 例: "VLC media player", "System" 等
            if tag == "menu":
                prefix = "[menu]"
            else:
                prefix = f"[{tag}]"

            lines.append(f"{prefix} \"{name}\" {self._format_center(n)}")
        return lines

    # ----------------------------
    # 圧縮：STATUSBAR（時間表示）
    # ----------------------------
    def _compress_statusbar(self, nodes: List[Node], w: int, h: int) -> List[str]:
        """
        VLC: 左が elapsed、右が duration になりがち。
        同名でも x で左右を分けて出す。
        """
        # label だけ抽出
        labels = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag not in ("label", "status", "statusbar"):
                continue
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue
            if name.lower() == "home":
                continue
            bbox = node_bbox_from_raw(n)
            labels.append((bbox["x"], n, name))

        labels.sort(key=lambda t: t[0])

        lines: List[str] = []
        if not labels:
            return lines

        # 左右2つが典型
        if len(labels) >= 2:
            left = labels[0]
            right = labels[-1]
            lines.append(f"[time-elapsed] \"{left[2]}\" {self._format_center(left[1])}")
            if right[1] is not left[1]:
                lines.append(f"[time-duration] \"{right[2]}\" {self._format_center(right[1])}")
            return lines

        # 1個しかない場合
        x, n, name = labels[0]
        lines.append(f"[status] \"{name}\" {self._format_center(n)}")
        return lines

    # ----------------------------
    # 圧縮：CONTENT（今は最小）
    # ----------------------------
    def _compress_content(self, nodes: List[Node]) -> List[str]:
        """
        VLC の基本画面は中央の情報が a11y に出ないことが多いので最小でOK。
        将来 playlist や drop-area が出るならここで拾う。
        """
        lines: List[str] = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            # ノイズ多いので、最初は代表的なものだけ
            if tag in ("label", "section", "heading", "paragraph"):
                lines.append(f"[{tag}] \"{name}\" {self._format_center(n)}")

        return lines


    # ----------------------------
    # Preference
    # ----------------------------
    def _compress_vlc_preferences(self, nodes: List[Node]) -> List[str]:
        """
        VLC Preferences 画面向けの圧縮:
        - まずノイズを落としてから XY ソート
        - label と control を同一行でペアリングして読みやすくする
        - 余り（チェックボックス等）は後段で列挙
        """
        # 1) ノイズ除去 & 対象タグに絞る
        filtered: List[Node] = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            name_lower = name.lower()

            # Unity の Home や time 表示などはノイズになりやすい
            if tag == "label" and name_lower in {"home", "--:--"}:
                continue

            # Preferences で意味があるものに限定（必要に応じて増やしてOK）
            if tag in {
                "label",
                "check-box",
                "combo-box",
                "spin-button",
                "push-button",
                "entry",
                "text",
                "radio-button",
                "table-cell",
            }:
                filtered.append(n)

        if not filtered:
            return []

        # 2) 画面上から下へ、左から右へソート
        filtered = self._sort_xy(filtered)

        # 3) ラベルとコントロールをペアにする（同一行 & 右側優先）
        pairs, leftover_controls, leftover_labels = self._pair_label_with_control(filtered, y_tol=18)

        lines: List[str] = []

        def fmt_node(n: Node) -> Tuple[str, str, Tuple[int, int]]:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            cx, cy = bbox_to_center_tuple(node_bbox_from_raw(n))

            if tag == "push-button":
                prefix = "[button]"
            elif tag == "combo-box":
                prefix = "[combo-box]"
            elif tag == "spin-button":
                prefix = "[spin]"
            elif tag == "check-box":
                prefix = "[check-box]"
            elif tag == "entry":
                prefix = "[entry]"
            elif tag == "text":
                prefix = "[text]"
            elif tag == "label":
                prefix = "[label]"
            elif tag == "radio-button":
                prefix = "[radio]"
            elif tag == "table-cell":
                prefix = "[table-cell]"
            else:
                prefix = f"[{tag}]"

            return prefix, name, (cx, cy)

        # 3-1) ペアを先に出す（視認性が一気に上がる）
        for ln, cn in pairs:
            l_name = (ln.get("name") or ln.get("text") or "").strip()
            c_prefix, c_name, (cx, cy) = fmt_node(cn)

            # control 側の表示名が label と同じ（spin/button等）なら空でもOK運用にしたい場合はここで調整
            lines.append(f'[field] "{l_name}" -> {c_prefix} "{c_name}" @ ({cx}, {cy})')

        for ln in leftover_labels:
            text = (ln.get("name") or ln.get("text") or "").strip()
            if not text:
                continue
            if len(text) >= 10 or "settings" in text.lower() or "&" in text:
                lines.append(f'[label] "{text}" {self._format_center(ln)}')

        # 3-2) 残りを列挙（特にチェックボックス・ボタンは重要）
        for n in leftover_controls:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            name_lower = name.lower()
            if tag == "label" and name_lower in {"home", "--:--"}:
                continue

            prefix, name, (cx, cy) = fmt_node(n)

            # label 単体は出しすぎるとうるさいので、必要ならフィルタ強めてもOK
            lines.append(f'{prefix} "{name}" @ ({cx}, {cy})')

        return lines


    @staticmethod
    def _sort_xy(nodes: List[Node]) -> List[Node]:
        def key(n: Node):
            b = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(b)
            return (cy, cx)
        return sorted(nodes, key=key)


    @staticmethod
    def _pair_label_with_control(nodes: List[Node], y_tol: int = 18):
        """
        label と control を同一行でペアリングする。
        - ラベルの右側にある最も近い control を対応付け
        - 使われなかった control は leftovers に返す
        """
        labels = []
        controls = []

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or n.get("text") or "").strip()
            if not name:
                continue

            b = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(b)

            if tag == "label":
                labels.append((n, cx, cy))
            elif tag in {"combo-box", "spin-button", "check-box", "push-button", "entry", "text", "radio-button", "table-cell"}:
                controls.append((n, cx, cy))

        pairs = []
        used_controls = set()

        # ラベルを上から順に見て、右側で同じ行の最も近い control を紐付け
        for ln, lx, ly in labels:
            best = None
            best_dx = 1e18
            for cn, cx, cy in controls:
                if id(cn) in used_controls:
                    continue
                if abs(cy - ly) <= y_tol and cx > lx:
                    dx = cx - lx
                    if dx < best_dx:
                        best_dx = dx
                        best = cn
            if best is not None:
                used_controls.add(id(best))
                pairs.append((ln, best))

        leftover_controls = [cn for (cn, _, _) in controls if id(cn) not in used_controls]
        # ★追加：ペアにならなかった label を返す
        paired_label_ids = {id(ln) for ln, _ in pairs}
        leftover_labels = [ln for (ln, _, _) in labels if id(ln) not in paired_label_ids]

        return pairs, leftover_controls, leftover_labels


    # ----------------------------
    # MODAL
    # ----------------------------
    def _compress_modal(self, nodes: List[Node], w: int, h: int) -> List[str]:
        return self.process_region_lines(nodes, w, h)


    def _filter_modal_nodes(self, modal_nodes: List[Node], w: int, h: int) -> List[Node]:
        """
        VLC:
        - Directory chooser は残す
        - combo-box のドロップダウン候補(list-item群)は除外する
        - bottom-right に出る不要な "Home" ラベルは常に除外する
        """
        if not modal_nodes:
            return []

        filtered: List[Node] = []

        for n in modal_nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").strip().lower()
            text = (n.get("text") or "").strip().lower()

            # -------------------------------------------------
            # (0) VLC 固有ノイズ: bottom-right の "Home" ラベル
            # -------------------------------------------------
            if tag in {"label", "text"} and (name == "home" or text == "home"):
                continue

            # bbox 取得
            try:
                b = node_bbox_from_raw(n)
                cx, cy = bbox_to_center_tuple(b)
            except Exception:
                # bbox 取れないやつは安全側で残す
                filtered.append(n)
                continue

            # ----------------------------
            # (A) ドロップダウン候補(list-item)を除外
            # ----------------------------
            if tag == "list-item":
                # combo-box の候補リストは左上に固まりがち
                is_dropdown_candidate = (
                    cx < w * 0.45 and   # 画面左寄り
                    cy < h * 0.35       # 画面上寄り
                )
                if is_dropdown_candidate:
                    continue  # ← 捨てる

            # ----------------------------
            # (B) それ以外は残す
            # ----------------------------
            filtered.append(n)

        return filtered



    


    def _looks_like_vlc_preferences(self, nodes: List[Node]) -> bool:
        texts = [self._disp(n) for n in nodes if self._disp(n)]

        # Preferences 本体の確定ワード（これがあれば優先して Preferences 扱い）
        keys_exact = {"reset preferences", "save", "cancel", "preferences"}
        hits = sum(1 for t in texts if t in keys_exact)
        hits += sum(1 for t in texts if "input & codecs settings" in t)

        return hits >= 2

    def _looks_like_vlc_advanced_settings(self, nodes: List[Node]) -> bool:
        texts = [self._disp(n) for n in nodes if self._disp(n)]
        if any("advanced settings" in t for t in texts):
            return True
        # table-cell が一定数あるなら Advanced settings の左カテゴリツリー濃厚
        table_cells = sum(1 for n in nodes if (n.get("tag") or "").lower() == "table-cell")
        return table_cells >= 8

    def _disp(self, n: Node) -> str:
        return (n.get("name") or n.get("text") or "").strip().lower()

    def _filechooser_markers(self) -> Set[str]:
        return {
            "look in:",
            "files of type:",
            "parent directory",
            "create new folder",
            "list view",
            "detail view",
            "choose",
            "directories",
        }

    def _looks_like_filechooser(self, nodes: List[Node]) -> bool:
        texts = [self._disp(n) for n in nodes if self._disp(n)]
        markers = self._filechooser_markers()
        hits = sum(1 for t in texts if t in markers)
        return hits >= 2

    def _is_filechooser_node(self, n: Node, w: int, h: int, filechooser_present: bool) -> bool:
        """
        filechooser が出てるときだけ使う「ノード単位の振り分け」。
        - marker一致は最優先で chooser
        - table/list は座標で chooser にしてOK
        - label/combo/button は「chooserっぽい上下帯」にいる時だけ chooser（Preferencesと被るのを避ける）
        """
        if not filechooser_present:
            return False

        tag = (n.get("tag") or "").lower()
        t = self._disp(n)

        # 1) marker一致は即 chooser
        if t in self._filechooser_markers():
            return True

        b = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(b)

        # chooser が出るおおよそのパネル範囲（※広めでもOK）
        in_panel = (w * 0.28) <= cx <= (w * 0.82) and (h * 0.18) <= cy <= (h * 0.86)
        if not in_panel:
            return False

        # 2) chooser の中身の主役（ファイル一覧/ツリー）は強制 chooser
        if tag in {"table-cell", "list-item"}:
            return True

        # 3) chooser の“上部ツールバー帯”“下部ボタン帯”だけ label/combo/button を拾う
        #    Preferences本体（中央帯）と被るのを避ける
        in_top_band = (h * 0.18) <= cy <= (h * 0.42)   # Look in / toolbar 付近
        in_bottom_band = (h * 0.62) <= cy <= (h * 0.86)  # Directory/Choose/Cancel 付近

        if tag in {"push-button", "label", "combo-box", "text", "entry"} and (in_top_band or in_bottom_band):
            return True

        return False



    def _make_pref_signature(self, nodes: List[Node]) -> Set[str]:
        """
        Preferences 継続判定のための “軽い指紋”。
        filechooser marker は除外して、Preferencesらしい語だけ残す。
        """
        ignore = self._filechooser_markers() | {"home", "--:--"}
        sig = set()
        for n in nodes:
            t = self._disp(n)
            if not t or t in ignore:
                continue
            sig.add(t)
        return sig




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

        # ==========================================
        # ★ 追加: 領域分割OFFの場合は、VLC専用の複雑な設定画面判定などをスキップして全データを出力
        # ==========================================
        if not getattr(self, "enable_region_segmentation", True):
            content_nodes = regions.get("CONTENT", [])
            if content_nodes:
                lines.extend(self.process_region_lines(content_nodes, screen_w, screen_h))
            
            if modal_nodes:
                lines.append("MODAL:")
                lines.extend(self.process_region_lines(modal_nodes, screen_w, screen_h))
                
            return lines
        # ==========================================

        # ----------------------------
        # VLC: Preferences 継続 / 置換 / 残差を MODAL に分離（★追加）
        # ----------------------------
        is_vlc_prefs = False
        pref_nodes: List[Node] = []
        modal_nodes = list(modal_nodes or [])

        # (A) 前回Preferencesがアクティブなら、modal_nodes から「前回に近い要素」をPreferencesとして回収
        if self._vlc_pref_active and modal_nodes:
            recovered_pref, remaining_modal = self._split_pref_by_prev(modal_nodes, tol_px=20)
            if recovered_pref:
                is_vlc_prefs = True
                pref_nodes = recovered_pref
                modal_nodes = remaining_modal  # 残りはMODALとして残す
            else:
                # ほぼ回収できないなら、Preferences継続は切る（必要ならここを緩めてOK）
                self._vlc_pref_active = False
                self._vlc_pref_prev = []

        # (B) まだPreferencesでないなら、今回の modal_nodes が Preferencesっぽいか判定（初回）
        if (not is_vlc_prefs) and modal_nodes and self._looks_like_vlc_preferences(modal_nodes):
            filechooser_present = self._looks_like_filechooser(modal_nodes)

            if filechooser_present:
                chooser_nodes: List[Node] = []
                base_pref_nodes: List[Node] = []
                for n in modal_nodes:
                    if self._is_filechooser_node(n, screen_w, screen_h, filechooser_present=True):
                        chooser_nodes.append(n)
                    else:
                        base_pref_nodes.append(n)

                if base_pref_nodes:
                    is_vlc_prefs = True
                    pref_nodes = base_pref_nodes
                    modal_nodes = chooser_nodes  # chooser だけ MODAL に残す
            else:
                is_vlc_prefs = True
                pref_nodes = modal_nodes
                modal_nodes = []


        # (B2) diffなし/初回で modal_nodes が空でも、CONTENT 側にPreferencesがあるなら拾う（★追加）
        if (not is_vlc_prefs):
            content_nodes = list((regions.get("CONTENT") or []))
            if content_nodes and self._looks_like_vlc_preferences(content_nodes):
                filechooser_present = self._looks_like_filechooser(content_nodes)

                if filechooser_present:
                    chooser_nodes: List[Node] = []
                    base_pref_nodes: List[Node] = []
                    for n in content_nodes:
                        if self._is_filechooser_node(n, screen_w, screen_h, filechooser_present=True):
                            chooser_nodes.append(n)
                        else:
                            base_pref_nodes.append(n)

                    if base_pref_nodes:
                        is_vlc_prefs = True
                        pref_nodes = base_pref_nodes

                        # ★Preferences を CONTENT から取り除く（CONTENT に label 群だけ残るのを防ぐ）
                        regions["CONTENT"] = []

                        # ★filechooser は MODAL 扱いに寄せたいなら、modal_nodes に追加
                        # （この時点で modal_nodes は空のことが多い）
                        modal_nodes = list(modal_nodes) + chooser_nodes
                else:
                    is_vlc_prefs = True
                    pref_nodes = content_nodes

                    # ★Preferences を CONTENT から取り除く
                    regions["CONTENT"] = []


        if is_vlc_prefs and modal_nodes:
            # filechooser は本物の modal として残したい
            if (not self._looks_like_filechooser(modal_nodes)) and self._looks_like_vlc_advanced_settings(modal_nodes):
                pref_nodes = list(pref_nodes) + list(modal_nodes)
                modal_nodes = []

        # (C) Preferences が確定したら、Preferences用ノードを保存して次回に備える
        if is_vlc_prefs:
            self._vlc_pref_active = True
            self._update_pref_prev(pref_nodes)
        else:
            self._vlc_pref_active = False
            self._vlc_pref_prev = []


        # SYSTEM(TOP_BAR)
        topbar_lines = self._compress_top_bar(regions.get("TOP_BAR", []))
        if topbar_lines:
            lines.append("=== SYSTEM ===")
            lines.extend(topbar_lines)
            lines.append("")

        # APP_LAUNCHER
        if regions.get("APP_LAUNCHER"):
            lines.append("=== LAUNCHER ===")
            lines.extend(self.process_region_lines(regions["APP_LAUNCHER"], screen_w, screen_h))
            lines.append("")

        # MENUBAR
        menubar_lines = self._compress_menubar(regions.get("MENUBAR", []))
        if menubar_lines:
            lines.append("=== MENUBAR ===")
            lines.extend(menubar_lines)
            lines.append("")

        # STATUSBAR
        status_lines = self._compress_statusbar(regions.get("STATUSBAR", []), screen_w, screen_h)
        if status_lines:
            lines.append("=== PLAYER STATUS ===")
            lines.extend(status_lines)
            lines.append("")

        # CONTENT / PREFERENCES
        if is_vlc_prefs:
            lines.append("=== VLC PREFERENCES ===")
            pref_lines = self._compress_vlc_preferences(pref_nodes)
            if pref_lines:
                lines.extend(pref_lines)
            lines.append("")
        else:
            content_lines = self._compress_content(regions.get("CONTENT", []))
            if content_lines:
                lines.append("=== CONTENT ===")
                lines.extend(content_lines)
                lines.append("")

        # MODAL / POPUP（Preferences本体は上で吸う。残り=FileChooser等がここに出る）
        if modal_nodes:
            modal_nodes = self._filter_modal_nodes(modal_nodes, screen_w, screen_h)
            if modal_nodes:
                lines.append("=== MODAL / POPUP ===")
                lines.extend(self._compress_modal(modal_nodes, screen_w, screen_h))

        return lines
