import re
from typing import List, Dict, Any

KNOWN_TAGS = {
    "label",
    "text",
    "static",
    "push-button",
    "check-box",
    "radio-button",
    "combo-box",
    "spin-button",
    "menu",
    "menu-item",
    "entry",
    "heading",
    "toggle-button",
    "link",
    "table-cell",
    "paragraph",
    "image",
    "scroll-bar",
    "list-item",
    "document-presentation",
    "document-frame",
    "terminal",   # ★ terminal も既知タグ
}

COORD_RE = re.compile(r"\(\s*\d+,\s*\d+\s*\)")

def parse_raw_a11y(text: str) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    last_node: Dict[str, Any] | None = None

    # ★ paragraph 用の pending。terminal はここに入れない
    pending_para_line: str | None = None

    for line in text.splitlines():
        original_line = line
        stripped = original_line.strip()
        if not stripped:
            continue

        # Header skip
        if stripped.startswith("LINEAR AT:") or stripped.startswith("PROPERTY:"):
            continue
        if stripped.startswith("tag\tname\t"):
            continue

        parts = original_line.split("\t")
        tag_candidate = parts[0].strip() if parts else ""

        # ============================
        # ★ pending paragraph がある場合だけ結合を試す
        # ============================
        if pending_para_line is not None:
            combined = pending_para_line + "\t" + stripped
            combined_parts = combined.split("\t")
            combined_tag = combined_parts[0].strip()

            # → 合体して「タグ＋5列以上＋座標あり」になったか？
            if (
                len(combined_parts) >= 5
                and combined_tag in KNOWN_TAGS
                and COORD_RE.search(combined)
            ):
                # 完全行として扱う
                parts = combined_parts
                original_line = combined
                stripped = combined.strip()
                tag_candidate = combined_tag
                pending_para_line = None
                # このまま下の通常処理へ
            else:
                # まだ座標が出てこない → pending を単独ノードとして確定
                tmp_parts = pending_para_line.split("\t")
                p_tag = tmp_parts[0].strip()
                p_name = tmp_parts[1].strip() if len(tmp_parts) > 1 else ""
                p_text = tmp_parts[2].strip() if len(tmp_parts) > 2 else ""

                node = {
                    "tag": p_tag,
                    "name": p_name,
                    "text": p_text,
                    "description": "",
                    "role": "",
                    "states": [],
                    "raw": pending_para_line.strip(),
                }
                nodes.append(node)
                last_node = node
                pending_para_line = None
                # 今の行はこのあと通常処理に回す（parts, tag_candidate は元のまま）

        # ============================
        # 通常の 1 行判定
        # ============================

        # 無効な "tag" 行を排除
        if tag_candidate.lower() == "tag":
            continue

        is_well_formed = (
            len(parts) >= 5 and
            tag_candidate in KNOWN_TAGS
        )

        if not is_well_formed:
            # ★ paragraph のみ pending に回す（terminal は含めない）
            if tag_candidate == "paragraph":
                pending_para_line = original_line
                continue

            # ★ 既知タグの不完全行 → 単独ノードとして扱う
            if tag_candidate in KNOWN_TAGS:
                name = parts[1].strip() if len(parts) > 1 else ""
                text_val = parts[2].strip() if len(parts) > 2 else ""
                desc = parts[3].strip() if len(parts) > 3 else ""
                role = parts[4].strip() if len(parts) > 4 else ""

                node: Dict[str, Any] = {
                    "tag": tag_candidate,
                    "name": name,
                    "text": text_val,
                    "description": desc,
                    "role": role,
                    "states": [],
                    "raw": stripped,  # ★ この時点では coords はまだない
                }
                nodes.append(node)
                last_node = node
                continue

            # ★ 未知タグ or タグなし → 直前ノードへの続き扱い
            if last_node is not None:
                text_cols = []
                coord_cols = []

                for col in parts:
                    col_s = col.strip()
                    if not col_s:
                        continue
                    if COORD_RE.match(col_s):
                        coord_cols.append(col_s)
                    else:
                        text_cols.append(col_s)

                merged = " ".join(text_cols).strip()
                if not merged:
                    # テキストなし・座標だけの行
                    if coord_cols:
                        raw = (last_node.get("raw") or "") + "\t" + "\t".join(coord_cols)
                        last_node["raw"] = raw
                    continue

                # ターミナルや paragraph は改行でつなぐ
                if last_node.get("tag") in ("paragraph", "terminal"):
                    sep = "\n"
                else:
                    sep = " "

                if last_node.get("text"):
                    last_node["text"] = (last_node["text"] + sep + merged).strip()
                else:
                    # paragraph / terminal は text に入れる方が自然
                    if last_node.get("tag") in ("paragraph", "terminal"):
                        last_node["text"] = merged
                    else:
                        last_node["name"] = (last_node.get("name", "") + sep + merged).strip()

                # 座標カラムがあれば raw にだけ追加
                if coord_cols:
                    raw = last_node.get("raw") or ""
                    last_node["raw"] = raw + "\t" + "\t".join(coord_cols)

            continue

        # ============================
        # 完全行 → ノード化
        # ============================

        tag = tag_candidate
        name = parts[1].strip() if len(parts) > 1 else ""
        text_val = parts[2].strip() if len(parts) > 2 else ""
        desc = parts[3].strip() if len(parts) > 3 else ""
        role = parts[4].strip() if len(parts) > 4 else ""

        states = []
        if len(parts) > 7:
            raw_states = parts[7].strip()
            if raw_states:
                states = [s.strip() for s in raw_states.split(",")]

        # paragraph / terminal の description も text に統合する
        if (tag in {"paragraph", "terminal"}) and desc:
            if text_val:
                text_val = (text_val + " " + desc).strip()
            else:
                text_val = desc
            desc = ""

        node = {
            "tag": tag,
            "name": name,
            "text": text_val,
            "description": desc,
            "role": role,
            "states": states,
            "raw": original_line.rstrip("\n"),
        }
        nodes.append(node)
        last_node = node

    # ============================
    # 最後に pending paragraph が残っていたら確定させる
    # ============================
    if pending_para_line is not None:
        tmp_parts = pending_para_line.split("\t")
        p_tag = tmp_parts[0].strip()
        p_name = tmp_parts[1].strip() if len(tmp_parts) > 1 else ""
        p_text = tmp_parts[2].strip() if len(tmp_parts) > 2 else ""
        node = {
            "tag": p_tag,
            "name": p_name,
            "text": p_text,
            "description": "",
            "role": "",
            "states": [],
            "raw": pending_para_line.strip(),
        }
        nodes.append(node)

    return nodes
