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
        self._vlc_pref_sig: Set[str] = set()   # Preferencesっぽい要素の名前集合（簡易シグネチャ）


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

    # ----------------------------
    # 表示用フォーマット
    # ----------------------------
    def _format_center(self, n: Node) -> str:
        bbox = node_bbox_from_raw(n)
        cx, cy = bbox_to_center_tuple(bbox)
        return f"@ ({cx}, {cy})"

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
            }:
                filtered.append(n)

        if not filtered:
            return []

        # 2) 画面上から下へ、左から右へソート
        filtered = self._sort_xy(filtered)

        # 3) ラベルとコントロールをペアにする（同一行 & 右側優先）
        pairs, leftovers = self._pair_label_with_control(filtered, y_tol=18)

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
            else:
                prefix = f"[{tag}]"

            return prefix, name, (cx, cy)

        # 3-1) ペアを先に出す（視認性が一気に上がる）
        for ln, cn in pairs:
            l_name = (ln.get("name") or ln.get("text") or "").strip()
            c_prefix, c_name, (cx, cy) = fmt_node(cn)

            # control 側の表示名が label と同じ（spin/button等）なら空でもOK運用にしたい場合はここで調整
            lines.append(f'[field] "{l_name}" -> {c_prefix} "{c_name}" @ ({cx}, {cy})')

        # 3-2) 残りを列挙（特にチェックボックス・ボタンは重要）
        for n in leftovers:
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
            elif tag in {"combo-box", "spin-button", "check-box", "push-button", "entry", "text"}:
                controls.append((n, cx, cy))

        pairs = []
        used = set()

        # ラベルを上から順に見て、右側で同じ行の最も近い control を紐付け
        for ln, lx, ly in labels:
            best = None
            best_dx = 1e18

            for cn, cx, cy in controls:
                if id(cn) in used:
                    continue
                if abs(cy - ly) <= y_tol and cx > lx:
                    dx = cx - lx
                    if dx < best_dx:
                        best_dx = dx
                        best = cn

            if best is not None:
                used.add(id(best))
                pairs.append((ln, best))

        leftovers = [cn for (cn, _, _) in controls if id(cn) not in used]
        return pairs, leftovers


    # ----------------------------
    # MODAL
    # ----------------------------
    def _compress_modal(self, nodes: List[Node], w: int, h: int) -> List[str]:
        return self.process_region_lines(nodes, w, h)


    def _filter_modal_nodes(self, modal_nodes: List[Node], w: int, h: int) -> List[Node]:
        """
        VLC: まずは何もしない。必要になったらここに
        - 上部メニューの複製除外
        - 画面外/異常座標除外
        などを追加する。
        """
        return list(modal_nodes or [])

    


    def _looks_like_vlc_preferences(self, nodes: List[Node]) -> bool:
        def disp(n: Node) -> str:
            return (n.get("name") or n.get("text") or "").strip().lower()

        texts = [disp(n) for n in nodes if disp(n)]

        # --- 1) FileChooser / Directory chooser を検知したら Preferences 扱いしない ---
        filechooser_markers = {
            "look in:",
            "files of type:",
            "parent directory",
            "create new folder",
            "list view",
            "detail view",
            "choose",
            "directories",
        }
        filechooser_hits = sum(1 for t in texts if t in filechooser_markers)
        if filechooser_hits >= 2:
            return False

        # --- 2) Preferences 本体の確定ワード ---
        keys_exact = {"reset preferences", "save", "cancel", "preferences"}
        hits = sum(1 for t in texts if t in keys_exact)

        # セクション名（部分一致）
        hits += sum(1 for t in texts if "input & codecs settings" in t)

        return hits >= 2

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


        # ----------------------------
        # VLC: Preferences 継続 / 置換 / 残差を MODAL に分離（★追加）
        # ----------------------------
        pref_nodes: List[Node] = []
        remain_modal: List[Node] = list(modal_nodes or [])

        if modal_nodes:
            filechooser_present = self._looks_like_filechooser(modal_nodes)

            # (A) Preferences “開始” 判定
            starts_pref = (not filechooser_present) and self._looks_like_vlc_preferences(modal_nodes)

            # (B) Preferences “継続” 判定（filechooser が混ざってても救う）
            # - 前回 prefs が active なら、今回 modal の中に prefs の指紋が少しでも残ってれば継続扱い
            curr_sig_all = self._make_pref_signature(modal_nodes)
            overlap = len(curr_sig_all & (self._vlc_pref_sig or set()))
            continues_pref = self._vlc_pref_active and (overlap >= 2)

            if starts_pref or continues_pref:
                # Preferences として取り込む（filechooserノードは除外）
                pref_nodes = [n for n in modal_nodes if not self._is_filechooser_node(n, screen_w, screen_h, filechooser_present)]

                # “置換”：Preferences のシグネチャを今回のものに更新
                self._vlc_pref_active = True
                self._vlc_pref_sig = self._make_pref_signature(pref_nodes)

                # 置換できなかった（= prefsに入らなかった）ものを新規 MODAL とみなす
                pref_ids = {id(n) for n in pref_nodes}
                remain_modal = [n for n in modal_nodes if id(n) not in pref_ids]
            else:
                # Preferences じゃない modal。prefs 状態は落としてOK（必要なら保持でも良い）
                self._vlc_pref_active = False
                self._vlc_pref_sig = set()
                pref_nodes = []
                remain_modal = list(modal_nodes)

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
        if pref_nodes:
            lines.append("=== VLC PREFERENCES ===")
            lines.extend(self._compress_vlc_preferences(pref_nodes))
            lines.append("")
        else:
            content_lines = self._compress_content(regions.get("CONTENT", []))
            if content_lines:
                lines.append("=== CONTENT ===")
                lines.extend(content_lines)
                lines.append("")

        # MODAL / POPUP（Preferences本体は上で吸い込むが、FileChooser等はここに残る）
        if remain_modal:
            remain_modal = self._filter_modal_nodes(remain_modal, screen_w, screen_h)
            if remain_modal:
                lines.append("=== MODAL / POPUP ===")
                lines.extend(self._compress_modal(remain_modal, screen_w, screen_h))


        return lines
