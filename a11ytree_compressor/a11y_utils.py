# a11y_utils.py
from typing import List, Dict, Any

def parse_raw_a11y(text: str) -> List[Dict[str, Any]]:
    nodes = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        
        # ヘッダー行スキップ (追加)
        if line.startswith("LINEAR AT:") or line.startswith("PROPERTY:"):
            continue
        if line.startswith("tag\tname\t"): # ★ これを追加
            continue

        parts = line.split("\t")
        
        tag  = parts[0].strip()
        # 万が一 "tag" という文字だけの行がきても弾けるように
        if tag.lower() == "tag": 
            continue

        name = parts[1].strip() if len(parts) > 1 else ""
        text_val = parts[2].strip() if len(parts) > 2 else ""
        desc = parts[3].strip() if len(parts) > 3 else ""
        role = parts[4].strip() if len(parts) > 4 else ""
        
        states = []
        if len(parts) > 7:
            raw_states = parts[7].strip()
            if raw_states:
                states = [s.strip() for s in raw_states.split(",")]

        nodes.append({
            "tag": tag,
            "name": name,
            "text": text_val,
            "description": desc,
            "role": role,
            "states": states,
            "raw": line,
        })
        
    return nodes