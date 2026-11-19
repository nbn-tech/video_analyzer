"""サムネット: スタジオのサムネ発表画面を見つけて、サムネを切り出す。

発表画面は「銀色の結晶風の背景＋金の額縁」で毎回同じデザイン。
額縁より上の帯（x720-1900, y0-80）を 0.5 秒ごとに小さく縮め、
#01 の発表画面から取った基準（reveal_ref.rgb）と色が近く、彩度が低い区間を発表画面とみなす。
CM をはさんで同じサムネが2回出ることがあるので、見た目が同じものは1枚にまとめる。

ffmpeg / ffprobe と Python 標準ライブラリだけで動く（GPU・AI 不要）。
#01（2026-11-18 調査）では 12 回検出し、重複を除いて 11 枚。誤検出・見落としなし。

使い方:
    python analysis/find_thumbnails.py "C:\\path\\#01.mp4" C:\\path\\thumbs_out
出力:
    thumb_01.jpg ...      切り出したサムネ（元の画質、1430x822）
    thumbnails.json       各サムネの表示区間（秒）と、2回目以降の表示区間
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REF_FILE = HERE / "reveal_ref.rgb"

FPS = 2
STRIP_W, STRIP_H = 59, 4
STRIP_FILTER = f"scale=1920:1080,crop=1180:80:720:0,scale={STRIP_W}:{STRIP_H},format=rgb24"
THUMB_CROP = "crop=1430:822:248:136"  # 額縁の内側（1920x1080 基準）

DIST_TH = 25.0  # 基準との平均色差（0-255）
SAT_TH = 30.0   # 平均彩度（max-min）。銀色の背景は低い
MIN_RUN_SEC = 1.0
DUP_TH = 12.0   # 縮小したサムネ同士の平均色差。これ未満は同じサムネとみなす

# 額縁の左右の金の枠（1920x1080 基準）。下の角はワイプが重なるので上の方だけ見る
FRAME_BORDERS = ("24:220:212:300", "24:220:1684:300")
GOLD_MIN_R = 150    # 金の枠の平均 R の下限
GOLD_MIN_RB = 55    # 金の枠の平均 (R - B) の下限。銀・灰色・空などは小さい


def _dist(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _sat(a: bytes) -> float:
    s = 0
    for i in range(0, len(a), 3):
        r, g, b = a[i], a[i + 1], a[i + 2]
        s += max(r, g, b) - min(r, g, b)
    return s / (len(a) / 3)


def _scan_runs(video: Path, ref: bytes) -> list[tuple[float, float]]:
    frame_size = STRIP_W * STRIP_H * 3
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(video),
           "-vf", f"fps={FPS},{STRIP_FILTER}", "-an", "-f", "rawvideo", "-"]
    runs: list[tuple[float, float]] = []
    cur: list[float] | None = None
    with subprocess.Popen(cmd, stdout=subprocess.PIPE) as proc:
        idx = 0
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            t = idx / FPS
            if _dist(buf, ref) < DIST_TH and _sat(buf) < SAT_TH:
                cur = [t, t] if cur is None else [cur[0], t]
            elif cur is not None:
                runs.append((cur[0], cur[1]))
                cur = None
            idx += 1
    if cur is not None:
        runs.append((cur[0], cur[1]))
    if proc.returncode not in (0, None):
        sys.exit(f"ffmpeg が失敗しました (exit {proc.returncode})")
    return [(s, e + 1 / FPS) for s, e in runs if e - s >= MIN_RUN_SEC]


def _tiny_thumb(video: Path, sec: float) -> bytes:
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-ss", f"{sec:.2f}", "-i", str(video),
           "-frames:v", "1", "-vf", f"scale=1920:1080,{THUMB_CROP},scale=32:18,format=rgb24",
           "-f", "rawvideo", "-"]
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def _has_gold_frame(video: Path, sec: float) -> bool:
    """左右の枠がどちらも金色なら True。上の帯だけ似ている別の画面（ロケ・告知など）を除く。"""
    for crop in FRAME_BORDERS:
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-ss", f"{sec:.2f}", "-i", str(video),
               "-frames:v", "1", "-vf", f"scale=1920:1080,crop={crop},scale=1:1,format=rgb24",
               "-f", "rawvideo", "-"]
        r, _g, b = subprocess.run(cmd, check=True, capture_output=True).stdout[:3]
        if r < GOLD_MIN_R or r - b < GOLD_MIN_RB:
            return False
    return True


def _save_thumb(video: Path, sec: float, dest: Path) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", f"{sec:.2f}", "-i", str(video),
           "-frames:v", "1", "-vf", f"scale=1920:1080,{THUMB_CROP}", "-q:v", "2", str(dest)]
    subprocess.run(cmd, check=True)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    video = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    if not video.exists():
        sys.exit(f"動画が見つかりません: {video}")
    out_dir.mkdir(parents=True, exist_ok=True)

    ref = REF_FILE.read_bytes()
    runs = _scan_runs(video, ref)
    print(f"発表画面の候補: {len(runs)} 区間")

    kept: list[dict] = []
    for start, end in runs:
        mid = (start + end) / 2  # 表示し始めのズーム演出を避けて真ん中のフレームを使う
        if not _has_gold_frame(video, mid):
            print(f"  除外（金の枠なし）: {int(start//60):02d}:{start%60:04.1f} - {int(end//60):02d}:{end%60:04.1f}")
            continue
        tiny = _tiny_thumb(video, mid)
        dup = next((k for k in kept if _dist(k["_tiny"], tiny) < DUP_TH), None)
        if dup is not None:
            dup["also_shown"].append({"start_sec": start, "end_sec": end})
            continue
        n = len(kept) + 1
        name = f"thumb_{n:02d}.jpg"
        _save_thumb(video, mid, out_dir / name)
        kept.append({"index": n, "file": name, "start_sec": start, "end_sec": end,
                     "capture_sec": round(mid, 2), "also_shown": [], "_tiny": tiny})
        print(f"  {name}: {int(start//60):02d}:{start%60:04.1f} - {int(end//60):02d}:{end%60:04.1f}")

    for k in kept:
        k.pop("_tiny")
    (out_dir / "thumbnails.json").write_text(
        json.dumps({"video": video.name, "thumbnails": kept}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"サムネ {len(kept)} 枚を保存: {out_dir}")


if __name__ == "__main__":
    main()
