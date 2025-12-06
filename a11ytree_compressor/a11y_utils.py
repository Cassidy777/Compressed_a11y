# a11y_utils.py
from typing import List, Dict, Any

# a11y の tag として想定しているもの一覧
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
    # 必要になったらここに追加していく
}


def parse_raw_a11y(text: str) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    last_node: Dict[str, Any] | None = None

    for line in text.splitlines():
        original_line = line  # raw 用にそのまま保持
        stripped = original_line.strip()
        if not stripped:
            continue
        
        # ヘッダー行スキップ
        if stripped.startswith("LINEAR AT:") or stripped.startswith("PROPERTY:"):
            continue
        if stripped.startswith("tag\tname\t"):
            continue

        parts = original_line.split("\t")
        tag_candidate = parts[0].strip() if parts else ""

        # 万が一 "tag" だけの行が来ても弾く
        if tag_candidate.lower() == "tag":
            continue

        # ★「ちゃんとした1行=1ノード」かどうかの判定
        #   - カラム数が十分ある
        #   - tag が KNOWN_TAGS に含まれる
        is_well_formed = (
            len(parts) >= 5 and
            tag_candidate in KNOWN_TAGS
        )

        if not is_well_formed:
            # ★ ここが「途中行」をマージするロジック
            if last_node is not None:
                extra = stripped
                if extra:
                    # text がすでにあれば text に、なければ name に足す
                    if last_node.get("text"):
                        last_node["text"] = (last_node["text"] + " " + extra).strip()
                    else:
                        last_node["name"] = (last_node.get("name", "") + " " + extra).strip()
            else:
                # 先頭からいきなりおかしな行が来た場合は無視
                # print(f"[WARN] stray line in a11y: {original_line!r}")
                pass
            continue

        # ここから「正常な1行=1ノード」の処理
        tag  = tag_candidate
        name = parts[1].strip() if len(parts) > 1 else ""
        text_val = parts[2].strip() if len(parts) > 2 else ""
        desc = parts[3].strip() if len(parts) > 3 else ""
        role = parts[4].strip() if len(parts) > 4 else ""
        
        states = []
        if len(parts) > 7:
            raw_states = parts[7].strip()
            if raw_states:
                states = [s.strip() for s in raw_states.split(",")]

        node: Dict[str, Any] = {
            "tag": tag,
            "name": name,
            "text": text_val,
            "description": desc,
            "role": role,
            "states": states,
            "raw": stripped,  # or original_line でもOK（好み）
        }
        nodes.append(node)
        last_node = node

    return nodes
