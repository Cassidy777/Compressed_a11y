from typing import Optional, Dict, Any, Literal

# ユーティリティ (既存の場所に合わせてパス調整してください)
from ..a11y_utils import parse_raw_a11y
from ..domain_detector import detect_domain_from_nodes, _estimate_screen_size
from ..a11y_instruction_utils import get_instruction_keywords


# 新しいアーキテクチャのインポート
from ..core.engine import BaseA11yCompressor
from ..domains.chrome import ChromeCompressor
from ..domains.gimp import GimpCompressor
# 他ドメインは後で増やす:
from ..domains.libreoffice_calc import LibreOfficeCalcCompressor
# from ..domains.libreoffice_writer import LibreOfficeWriterCompressor
# from ..domains.libreoffice_impress import LibreOfficeImpressCompressor
# from ..domains.vlc import VlcCompressor


# 1) domain → Compressor クラスのマッピング
DOMAIN_COMPRESSORS = {
    "chrome": ChromeCompressor,
    "gimp": GimpCompressor,
    "libreoffice_calc": LibreOfficeCalcCompressor,
    # "libreoffice_writer": LibreOfficeWriterCompressor,
    # "libreoffice_impress": LibreOfficeImpressCompressor,
    # "vlc": VlcCompressor,
}


def compress_from_raw_a11y(
    raw_a11y: str,
    instruction: Optional[str] = None,
    mode: Literal["instruction", "observation"] = "instruction",
    compressor: Optional[BaseA11yCompressor] = None,
) -> Dict[str, Any]:
    # 1. パース
    nodes = parse_raw_a11y(raw_a11y)

    # 2. ドメイン検出
    domain = detect_domain_from_nodes(nodes)

    # 3. 画面サイズの推定
    screen_w, screen_h = _estimate_screen_size(nodes)

    # 4. ドメインごとの Compressor を選択
    CompressorCls = DOMAIN_COMPRESSORS.get(domain, BaseA11yCompressor)
    compressor: BaseA11yCompressor = CompressorCls()

    compressor.domain_name = domain

    # 4-1. 背景フィルタ / STATUSBAR フラグ
    if domain == "os":
        compressor.enable_background_filtering = False
        compressor.use_statusbar = False

    elif domain == "gimp":
        compressor.enable_background_filtering = True
        compressor.use_statusbar = True

    elif domain in ("libreoffice_calc", "libreoffice_writer", "libreoffice_impress", "vlc"):
        compressor.enable_background_filtering = True
        compressor.use_statusbar = True

    else:
        # それ以外（chrome など）
        compressor.enable_background_filtering = True
        compressor.use_statusbar = False

    # 4-2. multi-line 正規化 & static 行マージのフラグ
    if domain in ("gimp", "libreoffice_calc", "libreoffice_writer", "libreoffice_impress", "vlc"):
        # a11y がガタガタな系
        compressor.enable_multiline_normalization = True
        compressor.enable_static_line_merge = True

    elif domain in ("chrome", "os"):
        # 圧縮しすぎを避けたい系
        compressor.enable_multiline_normalization = False
        compressor.enable_static_line_merge = False

    else:
        # デフォルト
        compressor.enable_multiline_normalization = True
        compressor.enable_static_line_merge = True

    # 5. 実行
    use_instruction = (mode == "instruction")

    if use_instruction and instruction:
        instruction_keywords = get_instruction_keywords(instruction)
    else:
        instruction_keywords = set()

    result = compressor.compress(
        nodes,
        screen_w=screen_w,
        screen_h=screen_h,
        instruction=instruction or "",
        instruction_keywords=instruction_keywords,
        use_instruction=use_instruction,
    )
    print("[DEBUG] instruction in compress_from_raw_a11y:", repr(instruction))
    return result



    # 5. 実行
    use_instruction = (mode == "instruction")

    # instruction からキーワード集合を作る（instruction が空なら空セット）
    if use_instruction and instruction:
        instruction_keywords = get_instruction_keywords(instruction)
    else:
        instruction_keywords = set()

    result = compressor.compress(
        nodes,
        screen_w=screen_w,
        screen_h=screen_h,
        instruction=instruction or "",
        instruction_keywords=instruction_keywords,  # ★追加
        use_instruction=use_instruction,
    )
    print("[DEBUG] instruction in compress_from_raw_a11y:", repr(instruction))
    return result

