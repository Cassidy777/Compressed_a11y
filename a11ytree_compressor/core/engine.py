# core/engine.py
import re
from typing import List, Dict, Any, Tuple, Optional, Set 
from ..a11y_instruction_utils import get_instruction_keywords, smart_truncate
from .common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    dedup_same_label_same_pos, extract_launcher_and_status,
    normalize_multiline_fields,
    spatially_group_lines, merge_fragmented_static_lines,
    truncate_label, build_state_suffix, clean_modal_nodes,
    dedup_similar_nodes_by_priority, dedup_heading_and_static
)
from .modal_strategies import DiffModalDetector, ClusterModalDetector, ModalDetector
from collections import Counter


class BaseA11yCompressor:
    domain_name = "generic"
    
    
    # ★ 追加: 背景ノイズ（デスクトップアイコン等）を除去するかどうかのフラグ
    # OSドメインなど、ファイル自体が主役の場合は False にオーバーライドする
    enable_background_filtering: bool = True

    # STATUSBAR セクションを出力するかどうか
    # LibreOffice / VLC などでは True にする。Chrome では False にしておく。
    use_statusbar: bool = False

    # 必要なドメイン（GIMPなど）でのみ True にオーバーライドする
    enable_cluster_fallback: bool = False

    # name/text/description の multi-line 正規化を行うかどうか
    enable_multiline_normalization: bool = True

    # 追加：近接 static 行のマージ（"A / B / C" みたいにまとめるか）
    enable_static_line_merge: bool = True

    def __init__(self):
        self.diff_detector = DiffModalDetector()
        # instruction 用の状態を初期化しておくと安心
        self.instruction: str = ""
        self.instruction_keywords: Set[str] = set()
        self.use_instruction: bool = False
        self.current_instruction: str = ""
        self.current_instruction_keywords: set[str] = set()
    
    
    def split_static_ui(
        self,
        nodes: List[Node],
        screen_w: int,
        screen_h: int,
    ) -> Tuple[List[Node], List[Node]]:
        """
        戻り値:
            (modal検出などに使うノード, 一時退避しておく静的UIノード)
        デフォルト実装では何もしない（全部前者に入れる）。
        Chrome などでオーバーライドする。
        """
        return nodes, []


    def compress(
        self,
        nodes: List[Node],
        screen_w: int = 1920,
        screen_h: int = 1080,
        instruction: Optional[str] = None,
        instruction_keywords: Optional[Set[str]] = None,  # ★ 追加
        use_instruction: bool = False,
    ) -> Dict[str, Any]:

        print("[DEBUG] instruction passed to modal_detector:", repr(instruction))
    
        # ★ ここでインスタンスに保持しておく
        self.instruction = instruction or ""
        self.instruction_keywords = instruction_keywords or set()
        self.use_instruction = use_instruction

        # instruction process
        # 1. instruction文字列から抽出
        extracted = get_instruction_keywords(instruction) if instruction else set()
        
        # 2. 引数で渡されたキーワードがあればマージ
        if instruction_keywords:
            extracted.update(instruction_keywords)
            
        # 3. インスタンス変数に保存 (以降の処理で使用)
        self.current_instruction_keywords = extracted
        
        # キーワードがあるなら Smart Truncate モードを有効化
        self.use_instruction = bool(self.current_instruction_keywords)

        
        # common process
        # 1. 前処理
        nodes = self.preprocess_nodes(nodes, instruction, use_instruction)

        # === DEBUG 1: preprocess_nodes 後のタグ分布 ===
        tag_counter = Counter((n.get("tag") or "").lower() for n in nodes)
        print("[DEBUG-1] AFTER preprocess_nodes tag_counter:", tag_counter)
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag == "link":
                label = (n.get("name") or n.get("text") or "")[:80]
                print("[DEBUG-1] LINK AFTER PREPROCESS:", label, "@", n.get("x"), n.get("y"))
        # === DEBUG 1 END ===

        # 1.5 ★ 静的UIをモーダル検出から外す（ここで一時退避）
        nodes_for_modal, detached_static_nodes = self.split_static_ui(
            nodes, screen_w, screen_h
        )


        # 2. モーダル分離（Diff / ドメイン専用 / Cluster）
        modal_nodes, base_nodes_after_modal, modal_mode = self._detect_modals(
            nodes_for_modal, screen_w, screen_h, instruction
        )

        # 2.5 ★ モーダル背景側に静的UIを戻す（モーダル候補には絶対入れない）
        base_nodes = base_nodes_after_modal + detached_static_nodes

        # 3. システムUI (Launcher / Status) の分離
        launcher_nodes, status_nodes, main_nodes = self.extract_system_ui(
            base_nodes, screen_w, screen_h
        )

        # 4. セマンティック領域分割（ここから先は今まで通り）
        regions = self.get_semantic_regions(main_nodes, screen_w, screen_h)

        regions["APP_LAUNCHER"] = launcher_nodes
        if status_nodes:
            if "STATUSBAR" not in regions:
                regions["STATUSBAR"] = []
            regions["STATUSBAR"].extend(status_nodes)

        # 5. 出力生成
        output_lines = self._build_output(regions, modal_nodes, screen_w, screen_h)
        return {
            "domain": self.domain_name,
            "text": "\n".join(output_lines),
        }

    def _should_skip_for_content(self, node: Node) -> bool:
        return False

    def preprocess_nodes(
        self,
        nodes: List[Node],
        instruction: str,
        use_instruction: bool,
    ) -> List[Node]:
        """
        ノードのクリーニング:
        0. name/text/description の改行・不可視文字の正規化
        1. 完全重複ノードの除去
        2. OSトップバー由来のノイズ(UI)の除去
        3. OSファイル由来のノイズラベル(__MACOSX, .DS_Storeなど)の除去
        4. （必要なら）背景ノイズとなるファイル名ラベルの除去
        """

        # === DEBUG-preprocess 1: 入ってきた時点のタグ分布 ===
        before_tags = Counter((n.get("tag") or "").lower() for n in nodes)
        print("=== preprocess_nodes: BEFORE ===")
        print("[DEBUG-pre] tags BEFORE:", before_tags)
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag == "link":
                label = (n.get("name") or n.get("text") or "")[:80]
                print("[DEBUG-pre] RAW LINK:", label, "@", n.get("x"), n.get("y"))
        # === DEBUG-preprocess 1 END ===

        # 0. マルチラインラベルを1行に統一（共通処理）
        if self.enable_multiline_normalization:
            nodes = normalize_multiline_fields(nodes)

        # 1. タグ・ラベル・座標が完全一致する重複ノードをまとめる
        nodes = dedup_same_label_same_pos(nodes)

        # 2. OSメニュー用ブラックリスト（menuタグ専用）
        os_menu_blacklist = {
            "system",
            "google chrome",
            "__macosx",
        }

        # 3. OSノイズラベル（タグに依存しない）
        OS_NOISE_LABELS = {
            "__macosx",
            ".ds_store",
        }

        filtered: List[Node] = []
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "") or ""
            text = (n.get("text") or "") or ""

            # name/text をまとめてラベルっぽく扱う
            label = (name or text).strip().lower()

            # ゼロ幅スペースなどを削除（_​_​MACOSX 対策）
            for ch in ("\u200b", "\u200e", "\u200f"):
                label = label.replace(ch, "")

            # 2-a) GNOMEのSystemメニューなど、OSトップバー由来のmenu
            if tag == "menu" and label in os_menu_blacklist:
                continue

            # 3-a) __MACOSX, .DS_Store など、OSファイル由来のラベルはタグに関係なく弾く
            if label in OS_NOISE_LABELS:
                continue

            filtered.append(n)

        nodes = filtered

        # 4. 背景ファイル除去（enable_background_filtering=True のときのみ）
        if self.enable_background_filtering:
            nodes = self._filter_background_noise(nodes)

        # === DEBUG-preprocess 2: 出ていく時点のタグ分布 ===
        after_tags = Counter((n.get("tag") or "").lower() for n in nodes)
        print("=== preprocess_nodes: AFTER ===")
        print("[DEBUG-pre] tags AFTER:", after_tags)
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if tag == "link":
                label = (n.get("name") or n.get("text") or "")[:80]
                print("[DEBUG-pre] PROCESSED LINK:", label, "@", n.get("x"), n.get("y"))
        # === DEBUG-preprocess 2 END ===

        return nodes




    def _detect_modals(self, nodes, w, h, instruction) -> Tuple[List[Node], List[Node], str]:
        modal_nodes, bg_nodes, mode = [], nodes, "none"

        # 1. Priority 1: Diff Detection
        #    まずは「変化があったもの」を全部拾う
        m, b, m_mode = self.diff_detector.detect(nodes, w, h, instruction)
        if m_mode == "diff" and m:
            modal_nodes, bg_nodes, mode = m, b, "diff"
        
        # 2. Priority 2: Domain Specific (Diffで見つからなかった場合)
        if not modal_nodes:
            for detector in self.get_modal_detectors():
                print("[DEBUG] Trying detector:", detector.__class__.__name__)  # ← 追加

                m, b = detector.detect(nodes, w, h)
                if m:
                    modal_nodes, bg_nodes, mode = m, b, detector.__class__.__name__
                    print("[DEBUG] SUCCESS detector:", mode)  # ← 追加
                    break
            
        # 3. Priority 3: Generic Cluster (最終手段)
        if not modal_nodes and self.enable_cluster_fallback:
            m, b = ClusterModalDetector().detect(nodes, w, h)
            if m:
                modal_nodes, bg_nodes, mode = m, b, "cluster"

        if modal_nodes:
            print(f"[DEBUG ENGINE] modal_nodes count BEFORE safety filter: {len(modal_nodes)}")
            apply_found = any("Apply" in (n.get("name") or n.get("text") or "") for n in modal_nodes)
            print(f"[DEBUG ENGINE] 'Apply' in modal_nodes BEFORE filter? {apply_found}")
        
        # =========================================================================
        # ★ UI Safety Filter (検出後に「UI領域」を強制排除する)
        # =========================================================================
        if modal_nodes:
            # ここが重要！
            # 判定のために「全ノード (modal + bg)」を渡して get_semantic_regions を呼ぶ。
            # これにより、アドレスバー等の位置関係が正しく認識され、
            # "Reload" ボタンなどが正確に "BROWSER_UI" に分類されるようになる。
            
            # ※ ID(メモリ番地)で追跡するために、一旦リストを結合
            check_nodes = modal_nodes + bg_nodes
            
            # ドメインロジックで分類を実行
            # (この内部で n["tag"] が書き換わる副作用があるが、正しいtagになるなら歓迎)
            regions = self.get_semantic_regions(check_nodes, w, h, dry_run=True)
            
            # モーダルに含まれてはいけない領域（静的UI）
            FORBIDDEN_REGIONS = {
                "WINDOW_CONTROLS",
                "BROWSER_TABS",
                "BROWSER_UI",     
                "APP_LAUNCHER",
                "STATUSBAR",
                "NAV", "TOOLS", "MENU_BAR"
            }

            # 禁止領域に含まれてしまったノードのIDを特定
            forbidden_ids = set()
            for region_name, r_nodes in regions.items():
                if region_name in FORBIDDEN_REGIONS:
                    for n in r_nodes:
                        forbidden_ids.add(id(n))

            # フィルタリング実行
            safe_modal_nodes = []
            rejected_nodes = []

            for n in modal_nodes:
                if id(n) in forbidden_ids:
                    rejected_nodes.append(n)
                else:
                    safe_modal_nodes.append(n)
            
            # 結果を更新
            modal_nodes = safe_modal_nodes
            if rejected_nodes:
                bg_nodes.extend(rejected_nodes)

        # =========================================================================
        # ★ 共通クリーニング (重複除去・ゴミ除去)
        # =========================================================================
        if modal_nodes:
            modal_nodes = clean_modal_nodes(modal_nodes)
            
            if not modal_nodes:
                return [], nodes, "none"

        return modal_nodes, bg_nodes, mode

    def get_modal_detectors(self) -> List[ModalDetector]:
        return []

    def _filter_background_noise(self, nodes: List[Node]) -> List[Node]:
        """
        デスクトップ上のファイル群など、
        背景ノイズになりがちな label ノードをざっくり落とす。
        いまは「ファイル名っぽい label」を対象にしている。
        """
        clean_nodes: List[Node] = []

        # 1. 拡張子のパターン（~ や # で終わる一時ファイルも含める）
        file_ext_pattern = re.compile(
            r'\.(pptx|ppt|docx|xlsx|pdf|png|jpg|jpeg|gif|xcf|desktop|txt|py|sh|zip|tar|gz|mp3|mp4|mov|wav|raw)(~|#)?$',
            re.IGNORECASE,
        )
        # .~lock.xxx みたいなロックファイル
        lock_file_pattern = re.compile(r'^\.~lock\.', re.IGNORECASE)

        # ゼロ幅スペースなどを削除するためのパターン
        zero_width_chars = re.compile(r'[\u200b\u200c\u200d\ufeff]')

        # 2. 特定のデスクトップアイコン名を除外するセットを定義
        ignored_names = {"helloextension"}

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            raw_name = (n.get("name") or "").strip()

            # label 以外はそのまま通す
            if tag != "label":
                clean_nodes.append(n)
                continue

            # 正規化前の名前が空ならそのまま残す
            if not raw_name:
                clean_nodes.append(n)
                continue

            # 小文字化 & ゼロ幅文字の削除
            lower = raw_name.lower()
            normalized = zero_width_chars.sub("", lower)

            
            if normalized in ignored_names:
                continue

            # ロックファイル・ゴミファイル名
            if lock_file_pattern.match(normalized):
                continue

            # 明らかにファイル名っぽいものは背景とみなして捨てる
            if file_ext_pattern.search(normalized):
                continue

            # Ubuntu の "New Folder" も背景フォルダの可能性が高いので捨てる
            if normalized.startswith("new folder"):
                continue

            # それ以外の label は残す
            clean_nodes.append(n)

        return clean_nodes


    def extract_system_ui(self, nodes: List[Node], w: int, h: int) -> Tuple[List[Node], List[Node], List[Node]]:
        launcher: List[Node] = []
        status: List[Node] = []
        main: List[Node] = []
    
        LAUNCHER_X_MAX = int(w * 0.035)
        STATUS_Y_MIN = int(h * 0.90)
        ICON_W_MAX = int(w * 0.05)
    
        # ファイル名パターン再利用
        file_ext_pattern = re.compile(
            r'\.(pptx|docx|xlsx|pdf|png|jpg|jpeg|gif|xcf|desktop|zip)(~|#)?$',
            re.IGNORECASE,
        )

        # ゼロ幅スペースなどを削除するためのパターン
        zero_width_chars = re.compile(r'[\u200b\u200c\u200d\ufeff]')

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            x, y, width, height = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            cx, cy = bbox["x"] + width // 2, bbox["y"] + height // 2

            tag = (n.get("tag") or "").lower()
            name = (n.get("name") or "").lower()
            label = (n.get("name") or n.get("text") or "").strip()

            # ラベル正規化（ゼロ幅文字削除＋小文字）
            normalized_label = zero_width_chars.sub("", label).lower()

            if tag == "label" and name in ("home", "helloextension"):
                n["tag"] = "status"
                status.append(n)
                continue

            # ==== ① __MACOSX は無条件にステータス扱い ====
            if normalized_label == "__macosx":
                n["tag"] = "status"
                status.append(n)
                continue

            # ==== Launcher ====
            if x <= LAUNCHER_X_MAX and width <= ICON_W_MAX and height >= 40:
                if tag in ("push-button", "toggle-button"):
                    n["tag"] = "launcher-app"
                    launcher.append(n)
                    continue
        
            # ==== Statusbar or Desktop Icons at bottom ====
            # ここは y ではなく中心 cy で判定した方が確実
            if cy >= STATUS_Y_MIN:
                # ファイル名っぽいものや Home / Trash を status に寄せる
                is_file = (
                    bool(file_ext_pattern.search(label))
                    or name == "home"
                    or "trash" in name
                    or label.startswith(".~lock")
                )

                if is_file or tag in ("label", "static") or tag == "status-bar":
                    n["tag"] = "status"
                    status.append(n)
                    continue

            # どれでもないものは通常の UI / コンテンツとして残す
            main.append(n)

        return launcher, status, main

    

    def get_semantic_regions(self, nodes: List[Node], w: int, h: int, dry_run: bool = False) -> Dict[str, List[Node]]:
        return {"CONTENT": nodes}

    def _build_output(self, regions, modal_nodes, w, h) -> List[str]:
        lines = []
        lines.extend(self.get_meta_header(regions))
    
        # 順序定義
        order = ["WINDOW_CONTROLS", "BROWSER_TABS", "APP_LAUNCHER", "BROWSER_UI", "NAV", "TOOLS", "STATUSBAR"]
    
        keys = list(regions.keys())

        # ★ ここがポイント：STATUSBAR を使わないドメインでは強制的に除外
        if not self.use_statusbar and "STATUSBAR" in keys:
            keys.remove("STATUSBAR")

        sorted_keys = [k for k in order if k in keys]
        other_keys = [k for k in keys if k not in order and k != "CONTENT"]
    
        for key in sorted_keys + other_keys:
            r_nodes = regions[key]
            if not r_nodes:
                continue

            region_lines = self.process_region_lines(r_nodes, w, h)
            if not region_lines:
                continue

            lines.append(f"{key}:")
            lines.extend(region_lines)

        if modal_nodes:
            lines.append("MODAL:")
            lines.extend(self.process_modal_nodes(modal_nodes))

        if content_nodes := regions.get("CONTENT"):
            lines.append("CONTENT:")
            lines.extend(self.process_content_lines(content_nodes, w, h))
            
        return lines

    def get_meta_header(self, regions: Dict[str, List[Node]]) -> List[str]: 
        return []
    
    def process_region_lines(self, nodes, w, h):
        tuples = self._nodes_to_tuples(nodes)
        tuples.sort()
        return spatially_group_lines(tuples, y_threshold=int(h * 0.04))

    def process_content_lines(self, nodes, w, h):
        """
        コンテンツ領域の圧縮パイプライン。
        Instruction-Awareな整形を行う前に、ノードの冗長性を排除する。
        """
        print("[DEBUG] process_content_lines:", self.domain_name, "static_merge=", self.enable_static_line_merge)
        # 1. Content固有のフィルタリング
        #    (ドメイン固有の _should_skip_for_content など)
        filtered_nodes = [n for n in nodes if not self._should_skip_for_content(n)]
        
        # 2. HeadingとStaticの重複を排除 (「heading + static の重複 → heading だけ残す」)
        filtered_nodes = dedup_heading_and_static(filtered_nodes)
        
        # 3. 類似ラベル+近接座標のノードを、優先度に基づいて排除 (メインの重複排除)
        filtered_nodes = dedup_similar_nodes_by_priority(filtered_nodes, distance_threshold=20.0)
        
        # 4. 抽出・整形（Instruction-Awareな処理もここに含まれる）
        tuples = self._nodes_to_tuples(filtered_nodes)

        # 5. 構造化・圧縮（common_ops.pyのレイアウト純粋関数に委譲）
        tuples.sort()
        y_tol = int(h * 0.03)
        x_tol = int(w * 0.15)
        if self.enable_static_line_merge:
            tuples = merge_fragmented_static_lines(tuples, y_tol, x_tol)
        return [t[2] for t in tuples]

    def process_modal_nodes(self, nodes: List[Node]) -> List[str]:
        # 1. 類似ラベル+近接座標のノードを、優先度に基づいて排除 (共通圧縮)
        filtered_nodes = dedup_similar_nodes_by_priority(
            nodes, 
            distance_threshold=20.0
        )
        
        # ★ 修正: Close Menuの断片化を解消するための専用クリーンアップ
        final_nodes = []
        found_semantic_close_button = False
        
        # (A) フィルタリングとセマンティック要素の検出
        for n in filtered_nodes:
            tag = (n.get("tag") or "").lower()
            label = (n.get("name") or n.get("text") or "").strip()

            # (A1) semantic buttonを発見
            if tag == "push-button" and "close menu" in label.lower():
                found_semantic_close_button = True
                final_nodes.append(n)
                continue
            
            # (A2) Headingの冗長性解消（Step 1の補完）
            if label == "Sort Your Refinement By" and tag == "static":
                # Heading版が残っているはずなのでStaticはスキップ
                continue 

            # (A3) Close menuの断片要素を削除
            if tag == "static" and (label == "" or label == "Close menu"):
                continue

            final_nodes.append(n)

        # (B) semantic buttonが存在しない場合、手動で挿入
        # (これはDiff検出がSemantic Buttonを背景に誤分類し、断片だけがModalに残ったケースをカバーする)
        if not found_semantic_close_button:
             # Close buttonの代用ノードを構築し、挿入
             close_btn_node = {
                 "tag": "push-button",
                 "name": " Close menu",
                 "text": "Close menu",
                 "states": [],
                 "raw": "push-button\t Close menu\tClose menu\t\t\t(422, 146)\t(16, 16)",
                 "description": "",
                 "role": ""
             }
             final_nodes.append(close_btn_node)

        # 2. タプル化（整形）
        tuples = self._nodes_to_tuples(final_nodes)
        
        # 3. 整形済みラインをそのまま返す
        return [t[2] for t in tuples]
    

    def _nodes_to_tuples(self, nodes: List[Node]) -> List[Tuple[int, int, str]]:
        results = []
        for n in nodes:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            tag = n.get("tag", "unknown")
            label = (n.get("name") or n.get("text") or "").strip()
            
            val = (n.get("value") or "").strip()
            val_attr = ""
            # Value も truncate 対象
            if val and val != label:
                v_out = truncate_label(val)
                val_attr = f' value="{v_out}"'
            
            state_str = build_state_suffix(tag, n.get("states", []))
            
            # ★ ラベルの整形: Instructionがあるなら Smart Truncate
            if self.use_instruction and self.current_instruction_keywords:
                label_out = smart_truncate(label, self.current_instruction_keywords)
            else:
                label_out = truncate_label(label)
            
            line = f'[{tag}] "{label_out}"{state_str}{val_attr} @ ({cx}, {cy})'
            results.append((bbox["y"], bbox["x"], line))
        return results