import os
import shutil
import json
import glob

# ================= CONFIGuration =================
# 抽出元のフォルダ（ログと画像がある場所）
# 例: .../vllm-qwen3vl32b/chrome/0d8b7de3-e8de-4d86-b9fd-dd2dce58a217
SOURCE_DIR = "/home/cassidy/AI_Agent/OSWorld_new/results/pyautogui/a11y_tree/vllm-qwen3vl32b/libreoffice_impress/0f84bef9-9790-432e-92b7-eece357603fb"
# 出力先の親ディレクトリ
DEST_DIR = "/home/cassidy/a11y-tree/demo_samples/libreoffice_impress/"

# 抽出したいステップの範囲 (N ~ M)
STEP_START = 1
STEP_END = 2

# Instructionが入っているJSONがあるルートディレクトリ
EXAMPLES_ROOT_PATH = "/home/cassidy/AI_Agent/OSWorld_new/evaluation_examples/examples"
# =================================================

def parse_runtime_log(log_path):
    """runtime.logを解析してステップごとのテキストブロックを辞書で返す"""
    if not os.path.exists(log_path):
        print(f"Error: {log_path} が見つかりません。")
        return {}

    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()

    start_marker = "LINEAR AT: tag\tname\ttext\tclass\tdescription\tposition (top-left x&y)\tsize (w&h)"
    end_marker = "Generating via vLLM:"

    steps_data = {}
    current_step = 1
    
    search_pos = 0
    while True:
        start_idx = content.find(start_marker, search_pos)
        if start_idx == -1:
            break
        
        end_idx = content.find(end_marker, start_idx)
        if end_idx == -1:
            break

        block = content[start_idx:end_idx].strip()
        steps_data[current_step] = block
        
        current_step += 1
        search_pos = end_idx + len(end_marker)

    return steps_data

def get_next_folder_number(dest_dir):
    """出力先ディレクトリ内の最大の数字フォルダを取得し、次の番号を返す"""
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)
        return 1
    
    existing_dirs = [d for d in os.listdir(dest_dir) if os.path.isdir(os.path.join(dest_dir, d))]
    max_num = 0
    for d in existing_dirs:
        try:
            num = int(d)
            if num > max_num:
                max_num = num
        except ValueError:
            continue
    return max_num + 1

def get_instruction_text(source_path):
    """
    SOURCE_DIRのパス構造からアプリ名とタスクIDを特定し、
    対応するJSONからinstructionを取得する
    """
    # 末尾のスラッシュを除去して正規化
    norm_path = os.path.normpath(source_path)
    
    # パスから情報を抽出
    # 例: .../chrome/0d8b... -> task_id="0d8b...", domain="chrome"
    task_id = os.path.basename(norm_path)
    domain = os.path.basename(os.path.dirname(norm_path))
    
    # JSONファイルのパスを構築
    # 例: .../examples/chrome/0d8b....json
    json_path = os.path.join(EXAMPLES_ROOT_PATH, domain, f"{task_id}.json")
    
    print(f"Looking for JSON at: {json_path}") # デバッグ用表示

    if not os.path.exists(json_path):
        print(f"Warning: JSON file not found: {json_path}")
        return "Instruction file not found."

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # JSONの構造に応じてinstructionを取得
            if isinstance(data, dict):
                return data.get("instruction", "Instruction key not found in JSON.")
            elif isinstance(data, list) and len(data) > 0:
                # 配列の場合は最初の要素を見るなどの処理
                return data[0].get("instruction", "Instruction key not found in JSON list.")
            else:
                return "Unknown JSON format."
    except Exception as e:
        return f"Error reading json: {e}"

def main():
    print(f"Processing steps {STEP_START} to {STEP_END} from {SOURCE_DIR}...")

    # 1. ログデータの取得
    log_data = parse_runtime_log(os.path.join(SOURCE_DIR, "runtime.log"))

    # 2. Instructionの取得 (パス構造から特定するように変更)
    instruction_text = get_instruction_text(SOURCE_DIR)

    # 3. 出力先の開始番号決定
    current_output_num = get_next_folder_number(DEST_DIR)

    # 4. 指定範囲のステップを処理
    for step_num in range(STEP_START, STEP_END + 1):
        # 画像ファイルの検索 (例: step_3_xxxx.png)
        img_pattern = os.path.join(SOURCE_DIR, f"step_{step_num}_*.png")
        img_files = glob.glob(img_pattern)
        
        log_content = log_data.get(step_num)

        if not img_files:
            print(f"Skip Step {step_num}: 画像が見つかりません。")
            continue
        
        # ログがない場合でも画像があれば保存する場合
        if not log_content:
            print(f"Warning Step {step_num}: ログが見つかりません (画像のみ保存します)")
            log_content = "Log not found for this step."

        # 出力フォルダ作成
        target_dir = os.path.join(DEST_DIR, str(current_output_num))
        os.makedirs(target_dir, exist_ok=True)

        # A. 画像のコピー -> image.png
        shutil.copy2(img_files[0], os.path.join(target_dir, "image.png"))

        # B. ログの保存 -> a11y.txt
        with open(os.path.join(target_dir, "a11y.txt"), 'w', encoding='utf-8') as f:
            f.write(log_content)

        # C. Instructionの保存 -> instruction.txt
        with open(os.path.join(target_dir, "instruction.txt"), 'w', encoding='utf-8') as f:
            f.write(instruction_text)

        print(f"Saved Step {step_num} -> Output Folder {current_output_num}")
        current_output_num += 1

    print("Done.")

if __name__ == "__main__":
    main()