"""サムネット調査用: 動画1本に Whisper と OCR だけをかけ、結果を JSON に保存する。

- 今の video_analyzer の処理（app/services.py）をそのまま呼ぶ
- S3 には何も書かない、Gemini も呼ばない（料金はかからない）
- Whisper / OCR の設定（モデル・並列数・GPU）は .env / .env.local の本番値を使う

使い方（分析用PCで。毎日の分析が止まっている平日 6:00〜18:00 に実行する）:
    .venv\\Scripts\\python samunet\\samunet_probe.py "D:\\chocosamu\\#01.mp4" D:\\chocosamu\\probe_out

※ ファイル名に "worker" や "backfill" を入れないこと（平日 6:00 の stop_worker.ps1 が名前で止めるため）
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    # 引数のパスは移動前に絶対パスにしておく
    video = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    if not video.exists():
        sys.exit(f"動画が見つかりません: {video}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # .env / .env.local は今いるフォルダから読まれるので、video_analyzer 直下に移動してから読み込む
    os.chdir(REPO_ROOT)
    sys.path.insert(0, str(REPO_ROOT))
    from app import services

    t0 = time.time()
    print(f"開始: {video}")
    with ThreadPoolExecutor(max_workers=2) as ex:
        audio_future = ex.submit(services.transcribe_video, video)
        ocr_future = ex.submit(services._extract_ocr_rows, video)
        transcript = audio_future.result()
        ocr_rows = ocr_future.result()

    audio_rows = services._segment_rows(transcript)
    elapsed = time.time() - t0

    (out_dir / "audio_rows.json").write_text(
        json.dumps(audio_rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out_dir / "ocr_rows.json").write_text(
        json.dumps(ocr_rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"完了: {elapsed/60:.1f}分  audio_rows={len(audio_rows)}  ocr_rows={len(ocr_rows)}")
    print(f"出力: {out_dir}")


if __name__ == "__main__":
    main()
