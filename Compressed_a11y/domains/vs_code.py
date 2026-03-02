import re
from typing import List, Dict, Tuple, Set, Optional, Any
from collections import defaultdict

from ..core.engine import BaseA11yCompressor
from ..core.common_ops import (
    Node, node_bbox_from_raw, bbox_to_center_tuple,
    dedup_horizontal_menu_nodes,
)

# ----------------------------
# 文字列処理 / 記号判定
# ----------------------------

# 先頭にある PUA(私用領域)アイコン + 空白 を除去する正規表現
# 例: "New File..." -> "New File..."
_PUA_PREFIX_RE = re.compile(r"^[\uE000-\uF8FF\s]+")

# 文字列全体が記号/PUAのみか判定する正規表現
_GLYPH_ONLY_RE = re.compile(r"^[\s\uE000-\uF8FF]+$")

def _node_disp(n) -> str:
    """
    表示用テキストを取得する。
    修正: [entry]タグの場合は、name(ファイル名など)よりも text(中身) を優先して返す。
    """
    tag = (n.get("tag") or "").lower()
    
    # 1. entryタグで text(中身) がある場合はそれを優先 (エディタのコード本文など)
    if tag == "entry":
        val = n.get("text")
        if val and val.strip():
            cleaned = _PUA_PREFIX_RE.sub("", val)
            return cleaned.strip()

    # 2. 通常の優先順位
    raw = (n.get("name") or n.get("text") or n.get("description") or "")
    cleaned = _PUA_PREFIX_RE.sub("", raw)
    return cleaned.strip()

def _is_glyph_only(s: str) -> bool:
    """
    文字列が「アイコン/記号のみ」で構成されているか判定する。
    """
    if not s:
        return True
    # PUAアイコンのみ
    if _GLYPH_ONLY_RE.match(s):
        return True
    # 2文字以下の記号のみ (例: ">", "{}")
    if len(s) <= 2 and all(not ch.isalnum() for ch in s):
        return True
    return False

def _bbox_key(n) -> Tuple[int, int]:
    b = node_bbox_from_raw(n)
    return (int(b["x"]), int(b["y"]))

def _pos_key(n, tol: int = 3) -> Tuple[int, int]:
    b = node_bbox_from_raw(n)
    x = int(b["x"]); y = int(b["y"])
    return (x // tol, y // tol)  


def drop_glyph_dupes_same_bbox(nodes: List[dict]) -> List[dict]:
    """
    (Baseクラス等で使う汎用関数 - 今回のVS Code特化ロジックとは別だが残しておく)
    """
    groups: Dict[Tuple[int,int], List[dict]] = defaultdict(list)
    for n in nodes:
        groups[_pos_key(n, tol=2)].append(n)

    kept: List[dict] = []
    for k, g in groups.items():
        has_meaningful = any(
            (disp := _node_disp(n)) and (not _is_glyph_only(disp))
            for n in g
        )

        if not has_meaningful:
            kept.extend(g)
            continue

        g_kept = []
        for n in g:
            disp = _node_disp(n)
            t = (n.get("tag") or "").lower()
            if _is_glyph_only(disp) and t in {"static", "section"}:
                continue
            g_kept.append(n)

        if not g_kept:
            g_kept = [g[0]]

        kept.extend(g_kept)

    return kept


class Vs_codeCompressor(BaseA11yCompressor):
    domain_name = "vs_code"

    def preprocess_nodes(self, nodes: List[Node], *args, **kwargs) -> List[Node]:

        # ==========================================
        # ★ 追加: 冗長性削減(ノイズ除去)OFFなら、親クラスの処理も含めてそのまま返す
        # ==========================================
        if not getattr(self, "enable_redundancy_reduction", True):
            return nodes
        # ==========================================

        # 1. 親クラスの処理（あれば）
        try:
            nodes = super().preprocess_nodes(nodes, *args, **kwargs)
        except Exception:
            pass
        
        cleaned_nodes = []

        # 操作に関連するタグ（これらは記号だけでも残す）
        INTERACTIVE_TAGS = {
            "push-button", "toggle-button", "button", "link", 
            "entry", "check-box", "combo-box", "menu-item", "menu",
            "tab", "tree-item", "list-item", "scrollbar",
            "slider", "spin-button", "radio-button"
        }

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            
            # 【修正点】 description も含めて生テキストを取得（情報落ち防止）
            raw_text = (n.get("name") or n.get("text") or n.get("description") or "").strip()
            
            # 1. 操作系タグなら無条件で残す（アイコンボタン等を救済）
            if tag in INTERACTIVE_TAGS:
                cleaned_nodes.append(n)
                continue

            # 2. それ以外（static, section等）で、かつ記号のみならノイズとして削除
            #    VS Codeにおいては座標判定不要で一律削除してOK
            if _is_glyph_only(raw_text):
                continue
            
            # 3. テキストがある表示要素は残す
            cleaned_nodes.append(n)
            
        return cleaned_nodes

    # ----------------------------
    # セマンティック領域分割 (VS Code)
    # ----------------------------
    def get_semantic_regions(
        self, nodes: List[Node], w: int, h: int, dry_run: bool = False
    ) -> Dict[str, List[Node]]:

        # ==========================================
        # ★ 追加: 領域分割OFFなら全部CONTENTに入れて終わる
        # ==========================================
        if not getattr(self, "enable_region_segmentation", True):
            return {"CONTENT": nodes}

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
            "MODAL": [], 
        }

        w = max(1, int(w))
        h = max(1, int(h))

        def _tag(n: Node) -> str:
            return (n.get("tag") or "").lower()

        def _name(n: Node) -> str:
            return (n.get("name") or n.get("text") or "").strip()

        # しきい値定義
        LAUNCHER_X_MAX = w * 0.05
        TOP_Y_MAX = h * 0.20
        STATUS_Y_MIN = h * 0.96

        MENUBAR_Y_MAX = h * 0.12
        TABBAR_Y_MIN = h * 0.07
        TABBAR_Y_MAX = h * 0.16

        ACTIVITY_X_MIN = w * 0.02
        ACTIVITY_X_MAX = w * 0.08

        # ---------------------------------------------------------
        # 【修正点】Side Bar境界を 0.30w に拡大 (view非依存で左側をカバー)
        # ---------------------------------------------------------
        SIDEBAR_X_MAX = w * 0.30
        # SIDEBAR_X_MIN は削除 (Activity Barの右から自然に始まるものとする)

        BREAD_Y_MIN = h * 0.10
        BREAD_Y_MAX = h * 0.18

        FR_Y_MIN = h * 0.12
        FR_Y_MAX = h * 0.28
        FR_X_MIN = w * 0.60

        MENU_KEYWORDS = {
            "file", "edit", "selection", "view", "go", "run", "terminal", "help"
        }

        for n in nodes:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            x, y, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

            tag = _tag(n)
            name = _name(n)
            lname = name.lower()

            # 1) APP_LAUNCHER
            if x <= 5 and bw >= w * 0.03 and bh >= 30:
                if tag in ("push-button", "toggle-button", "launcher-app", "button"):
                    regions["APP_LAUNCHER"].append(n)
                    continue

            # 2) STATUSBAR
            if cy >= STATUS_Y_MIN:
                if bw >= w * 0.20 and bh <= h * 0.05:
                    regions["STATUSBAR"].append(n)
                continue

            # 3) MENUBAR
            if cy <= MENUBAR_Y_MAX:
                if tag in ("menu", "push-button") and lname in MENU_KEYWORDS:
                    regions["MENUBAR"].append(n)
                    continue

            # 4) ACTIVITY_BAR
            name = (n.get("name") or "").strip().lower()

            if cx <= ACTIVITY_X_MAX and MENUBAR_Y_MAX <= cy <= h * 0.97:
                if tag in ("section", "push-button"):
                    if ("manage" in name) or (name == "accounts"):
                        regions["ACTIVITY_BAR"].append(n)
                        continue
                    regions["ACTIVITY_BAR"].append(n)
                    continue

            # 5) TAB_BAR
            if TABBAR_Y_MIN <= cy <= TABBAR_Y_MAX:
                if tag in ("section", "push-button", "tab", "label", "static"):
                    if (
                        "close (ctrl+w)" in lname
                        or "welcome" in lname
                        or "visual studio code" in lname
                        or any(ext in lname for ext in (".py", ".txt", ".md", ".json", ".yaml", ".yml"))
                    ):
                        regions["TAB_BAR"].append(n)
                        continue

            # 6) BREADCRUMB
            if BREAD_Y_MIN <= cy <= BREAD_Y_MAX:
                if ("/" in name) or (lname in {"home", "user", "desktop"}) or ("" in name):
                    if tag in ("section", "static", "label", "text", "link"):
                        regions["BREADCRUMB"].append(n)
                        continue

            # 7) FIND_REPLACE
            if FR_Y_MIN <= cy <= FR_Y_MAX and cx >= FR_X_MIN:
                if tag in ("entry", "check-box", "push-button", "toggle-button", "label", "text", "section", "static"):
                    if any(k in lname for k in ("find", "replace", "match", "regex", "selection", "previous", "next", "toggle")):
                        regions["FIND_REPLACE"].append(n)
                        continue

            # 8) SIDE_BAR (General)
            # 0.35w以下の左側にあるリスト・ツリー的なものは SIDE_BAR へ
            if cx <= SIDEBAR_X_MAX and (MENUBAR_Y_MAX <= cy <= h * 0.93):
                # 【修正】entryタグで中身が長い(コード本文など)場合は、座標に関わらずCONTENT(EDITOR)扱いにする
                if tag == "entry":
                    txt = (n.get("text") or "")
                    # 改行を含む、またはある程度長いテキストを持つentryはエディタ本文とみなす
                    if "\n" in txt or len(txt) > 40:
                        regions["CONTENT"].append(n)
                        continue

                # 誤爆防止のためタグはある程度絞るが、構造要素は拾う
                if tag in ("section", "push-button", "toggle-button", "tree-item", "list-item", "heading", "label", "static", "text", "entry", "input"):
                    regions["SIDE_BAR"].append(n)
                    continue

            # 9) 残りは CONTENT
            regions["CONTENT"].append(n)

        # 後処理
        if regions["MENUBAR"]:
            regions["MENUBAR"] = dedup_horizontal_menu_nodes(regions["MENUBAR"])

        return regions


    # ----------------------------
    # 圧縮ロジック
    # ----------------------------
    # def _format_center(self, n: Node) -> str:
    #     bbox = node_bbox_from_raw(n)
    #     cx, cy = bbox_to_center_tuple(bbox)
    #     return f"@ ({cx}, {cy})"
    
    def _format_center(self, n: Node) -> str:
        """フラグに応じて、中心座標(圧縮ON)か、生BBox+詳細属性(圧縮OFF)を切り替えて返す"""
        bbox = node_bbox_from_raw(n)

        # engine.pyで追加したフラグをチェック
        if getattr(self, "enable_redundancy_reduction", True):
            cx, cy = bbox_to_center_tuple(bbox)
            return f"@ ({cx}, {cy})"
        else:
            desc = (n.get("description") or "").strip()
            desc_attr = f' desc="{desc}"' if desc else ""
            
            role = (n.get("role") or "").strip()
            role_attr = f' role="{role}"' if role else ""
            
            return f"{desc_attr}{role_attr} @ ({bbox['x']}, {bbox['y']}, {bbox['w']}, {bbox['h']})"


    def _compress_menubar(self, nodes: List[Node]) -> List[str]:
        deduped = dedup_horizontal_menu_nodes(nodes)
        order = ["File","Edit","Selection","View","Go","Run","Terminal","Help"]

        bucket = {}
        for n in deduped:
            name = _node_disp(n) # PUA除去済み
            if not name:
                continue
            bucket.setdefault(name.lower(), n)

        lines = []
        for k in order:
            n = bucket.get(k.lower())
            if n:
                lines.append(f'[menu] "{k}" {self._format_center(n)}')
        
        for low, n in bucket.items():
            if low not in {k.lower() for k in order}:
                name = _node_disp(n)
                lines.append(f'[menu] "{name}" {self._format_center(n)}')
        return lines


    def _compress_simple_list(self, nodes: List[Node], allow_tags: Optional[Set[str]] = None, max_items: int = 25) -> List[str]:
        # ----------------------------------------------------
        # Static重複排除ロジック (A1)
        # ----------------------------------------------------
        SEMANTIC_TAGS = {
            "heading", "push-button", "toggle-button", "button", 
            "link", "tab", "tree-item", "list-item", "entry", "label"
        }
        
        # 1. Semanticタグのテキストを収集
        existing_texts = set()
        for n in nodes:
            tag = (n.get("tag") or "").lower()
            text = _node_disp(n).lower()
            if not text:
                continue
            if allow_tags and tag not in allow_tags:
                continue
            if tag in SEMANTIC_TAGS:
                existing_texts.add(text)

        lines = []
        seen_keys = set()

        for n in nodes:
            tag = (n.get("tag") or "").lower()
            if allow_tags and tag not in allow_tags:
                continue
            
            name = _node_disp(n) # ここで先頭PUAアイコンは除去される
            if not name:
                continue
            
            # Static重複チェック
            if tag in {"static", "section", "paragraph", "text"}:
                if name.lower() in existing_texts:
                    continue

            key = (tag, name.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            
            lines.append(f'[{tag}] "{name}" {self._format_center(n)}')
            if len(lines) >= max_items:
                break
        return lines


    def _compress_statusbar(self, nodes: List[Node]) -> List[str]:
        allow = {"label", "text", "status", "statusbar", "push-button"}
        return self._compress_simple_list(nodes, allow_tags=allow, max_items=18)


    def _compress_editor(self, nodes: List[Node]) -> List[str]:
        allow = {"heading", "label", "text", "link", "push-button", "toggle-button", "tab", "paragraph", "list-item", "tree-item", "entry"}
        return self._compress_simple_list(nodes, allow_tags=allow, max_items=30)

    
    def _compress_content_by_view(self, nodes: List[Node], view_type: str) -> List[str]:
        view_type = view_type or "generic"

        if view_type == "welcome":
            allow = {"heading", "paragraph", "push-button", "link", "label", "text", "section", "static", "check-box", "toggle-button"}
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=40)

        if view_type == "settings":
            allow = {
                "entry", "heading", "label", "text", "push-button", 
                "check-box", "combo-box", "list-item", "tree-item",
                "section", "static", "paragraph"
            }
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=40)
            
        if view_type == "extensions":
            allow = {
                "entry", "push-button", "heading", "label", "paragraph", "link",
                "section", "static"
            }
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=30)
            
        if view_type == "extensions_detail":
            allow = {
                "heading", "push-button", "link", "label", "paragraph", "entry",
                "section", "static", "text"
            }
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=35)

        if view_type == "command_palette":
            allow = {"entry", "list-item", "label", "text", "static"}
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=20)

        if view_type == "editor":
            allow = {
                "entry", "label", "text", "heading", "paragraph", "push-button", "link",
                "document-web", "document-frame"
            }
            return self._compress_simple_list(nodes, allow_tags=allow, max_items=30)

        # generic fallback
        allow = {
            "heading", "label", "text", "push-button", "link", "entry", "section", 
            "check-box", "combo-box", "static", "paragraph", "document-web"
        }
        return self._compress_simple_list(nodes, allow_tags=allow, max_items=30)


    # ----------------------------
    # View 判定
    # ----------------------------
    def _detect_view_type(self, nodes: List[Node]) -> str:
        from collections import defaultdict

        def tag(n): 
            return (n.get("tag") or "").lower()

        def disp(n):
            return _node_disp(n) # PUA除去済みを使用

        def ldisp(n):
            return disp(n).lower()

        blob = " ".join(ldisp(n) for n in nodes if disp(n))

        # 1) STRONG GUARDS
        if ("type the name of a command" in blob or "show and run commands" in blob or blob.strip().startswith(">")):
            return "command_palette"

        for n in nodes:
            t = tag(n); s = ldisp(n)
            if t in {"heading", "tab", "label"} and s == "settings": return "settings"
            if t == "entry" and "search settings" in s: return "settings"

        for n in nodes:
            t = tag(n); s = ldisp(n)
            # Extension Marketplace List
            if t == "entry" and "search extensions" in s and "marketplace" in s: return "extensions"
            if "extensions: marketplace" in s: return "extensions"
            
            # 【追加】Extension Details
            if s in {"uninstall", "disable", "enable", "switch to pre-release version"}:
                return "extensions_detail"
            if s == "runtime status" or s == "feature contributions" or s == "changelog":
                return "extensions_detail"

        # 2) SCORE-BASED
        score: Dict[str, float] = defaultdict(float)

        for n in nodes:
            t = tag(n); s = ldisp(n)

            if s in {"visual studio code", "get started", "walkthroughs", "recent", "start"}:
                score["welcome"] += 3.0 if t == "heading" else 1.0
            if s in {"new file", "open file...", "open folder...", "clone git repository..."}:
                if t in {"push-button", "link", "label"}: score["welcome"] += 2.0

            if s.startswith("ln ") or " col " in s: score["editor"] += 2.5
            if s in {"utf-8", "lf", "crlf", "plain text"} or s.startswith("spaces:") or s.startswith("tab size:"): score["editor"] += 2.0
            if "editor is not accessible" in s or "enable screen reader optimized mode" in s: score["editor"] += 5.0

            if t in {"document-web", "document-frame"} and "visual studio code" in s: score["editor"] += 4.0
            if " - visual studio code" in s: score["editor"] += 3.0

            if s in {"user settings", "workspace settings", "text editor", "workbench", "window", "features", "application"}: score["settings"] += 1.5
            if s in {"font size", "font family", "tab size", "cursor style"}: score["settings"] += 1.5
            if t == "check-box" and ("settings" in blob or "search settings" in blob): score["settings"] += 0.5

            if s in {"installed", "recommended", "enabled", "disabled"}: score["extensions"] += 1.5
            if s in {"install", "uninstall", "reload required"} and t in {"push-button", "button"}: score["extensions"] += 2.5
            if "publisher" in s or "downloads" in s: score["extensions"] += 0.8
            
            if s in {"changelog", "feature contributions", "runtime status", "categories", "resources"}: score["extensions_detail"] += 2.0
            if s == "extension: ": score["extensions_detail"] += 2.0

        top = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
        if not top:
            return "generic"

        best_view, best_score = top[0]
        MIN_SCORE = 3.0
        if best_score < MIN_SCORE:
            is_editor_strong_evidence = (" - visual studio code" in blob) or any(
                (tag(n) in {"document-web", "document-frame"} and "visual studio code" in ldisp(n))
                for n in nodes
            )
            if is_editor_strong_evidence: return "editor"
            return "generic"

        return best_view


    # ----------------------------
    # Modal 救助 / マージ
    # ----------------------------
    def _rescue_from_modal(
        self,
        merged_modal: List[Node],
        regions: Dict[str, List[Node]],
        w: int,
        h: int,
        force_distribute: bool = False,
    ) -> Tuple[List[Node], List[Node]]:
        """
        diff-modal が誤って吸った要素を、幾何＋軽い文字判定で救助して適切な領域へ戻す。
        force_distribute=True の場合は、match_ratioや数が多いと判断されたため、
        Modalに残さず全て geometry ベースで SIDE_BAR / CONTENT に振り分ける。
        """
        rescued: List[Node] = []
        remain: List[Node] = []

        w = max(1, int(w))
        h = max(1, int(h))

        def _tag(n: Node) -> str:
            return (n.get("tag") or "").lower()

        def _name(n: Node) -> str:
            return _node_disp(n) # PUA除去済みを使用

        # しきい値
        STATUS_Y_MIN = h * 0.96
        MENUBAR_Y_MAX = h * 0.12
        TAB_Y_MIN, TAB_Y_MAX = h * 0.07, h * 0.16
        ACT_X_MIN, ACT_X_MAX = w * 0.02, w * 0.08
        FR_Y_MIN, FR_Y_MAX, FR_X_MIN = h * 0.12, h * 0.28, w * 0.60
        BREAD_Y_MIN, BREAD_Y_MAX = h * 0.10, h * 0.18

        # サイドバー領域 (Extensionsパネル等がここに入る)
        SIDEBAR_X_MAX = w * 0.30
        SIDEBAR_Y_MIN = h * 0.08 

        MENU_KEYWORDS = {"file", "edit", "selection", "view", "go", "run", "terminal", "help"}

        for n in merged_modal:
            bbox = node_bbox_from_raw(n)
            cx, cy = bbox_to_center_tuple(bbox)
            tag = _tag(n)
            name = _name(n)
            lname = name.lower()

            # ----------------------------------------------------
            # 【修正点】大量Modal検出時は強制的に左右へ振り分ける
            # ----------------------------------------------------
            if force_distribute:
                # 0.35w より左なら SIDE_BAR
                if cx <= SIDEBAR_X_MAX:
                    regions.setdefault("SIDE_BAR", []).append(n)
                    rescued.append(n)
                    continue
                # それ以外は CONTENT
                else:
                    regions.setdefault("CONTENT", []).append(n)
                    rescued.append(n)
                    continue

            # --- 通常救出ロジック ---

            # 1) STATUSBAR
            if cy >= STATUS_Y_MIN:
                regions.setdefault("STATUSBAR", []).append(n)
                rescued.append(n); continue

            # 2) MENUBAR
            if cy <= MENUBAR_Y_MAX and tag in ("menu", "push-button") and lname in MENU_KEYWORDS:
                regions.setdefault("MENUBAR", []).append(n)
                rescued.append(n); continue

            # 3) ACTIVITY_BAR
            if ACT_X_MIN <= cx <= ACT_X_MAX and tag in ("section", "push-button", "toggle-button", "static"):
                regions.setdefault("ACTIVITY_BAR", []).append(n)
                rescued.append(n); continue

            # 4) TAB_BAR
            if TAB_Y_MIN <= cy <= TAB_Y_MAX and (
                "close (ctrl+w)" in lname or "welcome" in lname or "visual studio code" in lname or 
                any(ext in lname for ext in (".py", ".txt", ".md", ".json", ".yaml", ".yml"))
            ):
                regions.setdefault("TAB_BAR", []).append(n)
                rescued.append(n); continue

            # 5) BREADCRUMB
            if BREAD_Y_MIN <= cy <= BREAD_Y_MAX and ("/" in name or lname in {"home", "user", "desktop"} or "" in name):
                regions.setdefault("BREADCRUMB", []).append(n)
                rescued.append(n); continue

            # 6) FIND_REPLACE
            if FR_Y_MIN <= cy <= FR_Y_MAX and cx >= FR_X_MIN:
                if tag in ("entry", "check-box", "push-button", "label", "text", "section", "static"):
                    if any(k in lname for k in ("find", "replace", "match", "regex", "previous", "next", "toggle", "selection")):
                        regions.setdefault("FIND_REPLACE", []).append(n)
                        rescued.append(n); continue

            # 7) SIDE_BAR (Extensions, ExplorerなどがModal扱いされた場合の救済)
            if cx <= SIDEBAR_X_MAX and cy >= SIDEBAR_Y_MIN:
                # 通知/Toast系は救出せず MODAL/Background に残す
                if any(k in lname for k in ("update", "notification", "error", "warning", "toast")):
                    remain.append(n)
                    continue

                if tag == "entry":
                     txt = n.get("text") or ""
                     if "\n" in txt or len(txt) > 40:
                         regions.setdefault("CONTENT", []).append(n)
                         rescued.append(n); continue

                if tag in ("tree-item", "list-item", "section", "push-button", "entry", "heading", "label", "static"):
                    regions.setdefault("SIDE_BAR", []).append(n)
                    rescued.append(n); continue

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

        # ==========================================
        # ★ 追加: 領域分割OFFの場合は、VS Code専用の複雑なView判定やModal救済をスキップして全データを出力
        # ==========================================
        if not getattr(self, "enable_region_segmentation", True):
            content_nodes = regions.get("CONTENT", [])
            if content_nodes:
                lines.extend(self.process_region_lines(content_nodes, screen_w, screen_h))
            
            if modal_nodes:
                lines.append("MODAL:")
                lines.extend(self.process_region_lines(modal_nodes, screen_w, screen_h))
                
            return lines
        # ===============

        # 1) merged modal
        merged_modal = list(regions.get("MODAL") or [])
        if modal_nodes:
            merged_modal.extend(list(modal_nodes))
        regions["MODAL"] = []

        # ----------------------------------------------------------------
        # 【修正点】大量のDiff Modalが発生している場合はModal扱いをキャンセルする
        # (match_ratio >= 0.7 相当の判定として、ノード数 40以上 を閾値とする)
        # ----------------------------------------------------------------
        is_massive_modal = (len(merged_modal) >= 40)

        # 2) MODAL救助
        if merged_modal:
            _rescued, remaining_modal = self._rescue_from_modal(
                merged_modal, regions, w, h, force_distribute=is_massive_modal
            )
        else:
            remaining_modal = []

        # 3) view判定
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

        if view_type == "command_palette":
            regions.setdefault("CONTENT", []).extend(remaining_modal)
            remaining_modal = []

        # ---- 出力 ----
        def _emit(title: str, nodes: List[Node], allow: Optional[Set[str]] = None, max_items: int = 18):
            chunk = self._compress_simple_list(nodes or [], allow_tags=allow, max_items=max_items)
            if chunk:
                lines.append(f"=== {title} ===")
                lines.extend(chunk)
                lines.append("")

        _emit("LAUNCHER", regions.get("APP_LAUNCHER", []), max_items=20)
        _emit("MENUBAR", regions.get("MENUBAR", []), allow={"menu", "push-button"}, max_items=12)
        _emit("ACTIVITY_BAR", regions.get("ACTIVITY_BAR", []), allow={"section", "push-button", "toggle-button", "static"}, max_items=14)
        _emit("SIDE_BAR", regions.get("SIDE_BAR", []), allow={"section", "tree-item", "list-item", "push-button", "heading", "label", "static", "text", "entry", "input"}, max_items=18)
        _emit("TAB_BAR", regions.get("TAB_BAR", []), allow={"section", "push-button", "tab", "label", "static"}, max_items=10)
        _emit("BREADCRUMB", regions.get("BREADCRUMB", []), allow={"section", "label", "text", "link", "static"}, max_items=10)
        _emit("FIND / REPLACE", regions.get("FIND_REPLACE", []), allow={"entry", "check-box", "push-button", "label", "text", "section", "static"}, max_items=20)

        # CONTENT
        content_lines = self._compress_content_by_view(regions.get("CONTENT", []), view_type=view_type)
        if content_lines:
            title_suffix = view_type.upper().replace("_", " ") if view_type != "generic" else "MAIN"
            lines.append(f"=== CONTENT ({title_suffix}) ===")
            lines.extend(content_lines)
            lines.append("")

        _emit("STATUSBAR", regions.get("STATUSBAR", []), allow={"push-button", "label", "text", "section", "static"}, max_items=14)

        # MODAL / NOTIFICATION
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