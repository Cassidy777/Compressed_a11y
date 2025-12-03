import sys
from pathlib import Path
from typing import Optional, List

# ============================================================
# ★ 変更点: 新しいディレクトリ構成に合わせてインポートを修正
# ============================================================
from a11ytree_compressor.a11y_utils import parse_raw_a11y
from a11ytree_compressor.domain_detector import detect_domain_from_nodes

# 以前は直下でしたが、pipelines の中へ移動したのでパスを変更
from a11ytree_compressor.pipelines.a11y_compress import (
    compress_from_raw_a11y,
    DOMAIN_COMPRESSORS,
)
from a11ytree_compressor.core.engine import BaseA11yCompressor



# ============================================================
# デフォルト sample_id / mode
# ============================================================
DEFAULT_SAMPLE_ID = 1
DEFAULT_MODE = "baseline"   # "baseline" or "instruction"


# ============================================================
# サンプル読み込み用のユーティリティ
# ============================================================

def load_a11y_sample(domain: str, sample_id: int) -> str:
    """
    demo_samples/<domain>/<sample_id>/a11y.txt を読み込む。
    例: demo_samples/chrome/1/a11y.txt
    """
    # run_demo.py が置かれている場所の demo_samples を探す
    base_dir = Path(__file__).parent / "demo_samples"
    sample_dir = base_dir / domain / str(sample_id)
    a11y_path = sample_dir / "a11y.txt"

    if not a11y_path.exists():
        print(f"[ERROR] a11y サンプルファイルが見つかりません: {a11y_path}")
        sys.exit(1)

    return a11y_path.read_text(encoding="utf-8")


def load_instruction_sample(domain: str, sample_id: int) -> Optional[str]:
    """
    demo_samples/<domain>/<sample_id>/instruction.txt があれば読み込む。
    instruction-mode テスト用。
    """
    base_dir = Path(__file__).parent / "demo_samples"
    sample_dir = base_dir / domain / str(sample_id)
    inst_path = sample_dir / "instruction.txt"

    if not inst_path.exists():
        # Instructionモード指定なのにファイルがない場合は警告だけ出して続行
        return None

    return inst_path.read_text(encoding="utf-8").strip()


# ============================================================
# メイン処理
# ============================================================

def main():
    # 引数チェック
    if len(sys.argv) < 2:
        print("[ERROR] ドメイン名が指定されていません。")
        print("使い方: python run_demo.py <domain> [sample_ids] [mode]")
        print("例:    python run_demo.py chrome 3,4 instruction")
        sys.exit(1)

    domain = sys.argv[1]
    output_dir = Path(__file__).parent / "demo_outputs"
    output_dir.mkdir(exist_ok=True)

    # --------------------------------------------------------
    # ★ ドメインに応じた compressor を 1 回だけ作成（ここがポイント）
    # --------------------------------------------------------
    CompressorCls = DOMAIN_COMPRESSORS.get(domain, BaseA11yCompressor)
    compressor = CompressorCls()

    # --------------------------------------------------------
    # sample_ids の解釈
    # --------------------------------------------------------
    sample_ids: List[int]
    arg_idx_mode = 2
    
    if len(sys.argv) >= 3:
        arg2 = sys.argv[2]
        if "," in arg2:
            # "1,2,3" 形式
            try:
                sample_ids = [int(s.strip()) for s in arg2.split(",") if s.strip()]
                arg_idx_mode = 3
            except ValueError:
                sample_ids = [DEFAULT_SAMPLE_ID]
        elif arg2.isdigit():
            # "1" 形式
            sample_ids = [int(arg2)]
            arg_idx_mode = 3
        else:
            # 第2引数が数字じゃないなら mode とみなす
            sample_ids = [DEFAULT_SAMPLE_ID]
            arg_idx_mode = 2
    else:
        sample_ids = [DEFAULT_SAMPLE_ID]

    # mode（baseline / instruction）の解釈
    mode = DEFAULT_MODE
    if len(sys.argv) > arg_idx_mode:
        mode_arg = sys.argv[arg_idx_mode].lower()
        if mode_arg in ("baseline", "instruction"):
            mode = mode_arg
        else:
            print(f"[WARN] 不明な mode '{mode_arg}'。'{DEFAULT_MODE}' で実行します。")
            mode = DEFAULT_MODE

    output_dir = Path(__file__).parent / "demo_outputs"
    output_dir.mkdir(exist_ok=True)

    # =====================================================
    # 実行ループ
    # =====================================================
    for sample_id in sample_ids:
        try:
            sample_text = load_a11y_sample(domain, sample_id)
        except Exception as e:
            print(e)
            continue

        instruction_text: Optional[str] = None
        if mode == "instruction":
            instruction_text = load_instruction_sample(domain, sample_id)
            if instruction_text is None:
                print(f"[INFO] ID={sample_id} は instruction.txt が無いため baseline 動作になります。")

        label = f"{domain}-{sample_id}"

        print("=" * 60)
        print(f"  SAMPLE: domain={domain}, id={sample_id}, mode={mode}")
        print("=" * 60)

        # ドメイン検出のテスト表示
        nodes = parse_raw_a11y(sample_text)
        detected_domain = detect_domain_from_nodes(nodes)
        print(f"DETECTED DOMAIN: {detected_domain}")

        # --- 圧縮実行 ---
        # pipelines/a11y_compress.py 側で画面サイズの自動推定が入っているので
        # ここではシンプルに呼び出すだけでOK
        compressed = compress_from_raw_a11y(
            sample_text,
            instruction=instruction_text,
            mode=mode,
            compressor=compressor,  
        )


        if isinstance(compressed, dict):
            compressed_text = compressed.get("text", str(compressed))
        else:
            compressed_text = str(compressed)

        print("\n=== COMPRESSED A11Y ===")
        print(compressed_text)

        # 保存
        out_filename = f"{domain}_{sample_id}_{detected_domain}_{mode}.txt"
        out_path = output_dir / out_filename

        with out_path.open("w", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write(f"SAMPLE: domain={domain}, id={sample_id} ({label}), mode={mode}\n")
            f.write("=" * 60 + "\n\n")

            f.write("ORIGINAL LINEAR A11Y (Head 500 chars):\n")
            f.write(sample_text[:500] + "...\n\n")

            if instruction_text:
                f.write("INSTRUCTION:\n")
                f.write(instruction_text.strip() + "\n\n")

            f.write(f"DETECTED DOMAIN: {detected_domain}\n\n")

            f.write("=== COMPRESSED A11Y ===\n")
            f.write(compressed_text.strip() + "\n")

        print(f"\n[SUCCESS] Saved to: {out_path.name}")


if __name__ == "__main__":
    main()