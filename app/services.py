import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import google.generativeai as genai
import whisper
from PIL import Image
from tqdm import tqdm

from app.config import settings

logger = logging.getLogger(__name__)

_PROMPT = """
You are a news video corner segmentation assistant.
Input has:
- audio_rows: Whisper rows with start_sec/end_sec/text
- ocr_rows: OCR rows with start_sec/end_sec/text
- boundary_candidates_sec: candidate boundary timestamps

Rules:
- One corner should contain one topic.
- Merge ALL weather forecast content (local weather, regional forecast, weekend forecast, weekly forecast, weather map, etc.) into one single corner titled "天気予報". Do not split weather into multiple corners.
- Separate unrelated topics into separate corners.
- In news programs, each news story that covers a different subject or event MUST be a separate corner. Do NOT merge two different news stories into one corner even if they appear back to back. When the topic changes (different event, different person, different location), always start a new corner.
- Studio commentary or reactions about the same news story should be merged into the same corner as that story, not split off.
- CM and commercial breaks: merge ALL consecutive CM segments into a single corner. Set the title to "CM" and the summary to "CM中". Do not describe CM content.
- Keep boundaries near candidate timestamps when possible.
- The corners must cover the ENTIRE video duration with NO gaps. Every second of the video must belong to exactly one corner. The end_sec of each corner must equal the start_sec of the next corner.
- Use OCR rows to supplement named entities, rankings, scores, and on-screen labels not captured in audio.
- OCR rows may contain noise; prioritize Japanese text and discard short or garbled tokens.
- Whisper transcriptions may contain recognition errors (e.g., wrong kanji homonyms). When OCR rows show more accurate text for the same timeframe, use the OCR text to produce correct, natural Japanese in titles and summaries. Do not blindly copy Whisper errors into summaries.

Return only JSON array:
[
  {
    "start_sec": 0.0,
    "end_sec": 123.4,
    "title": "Corner title in Japanese",
    "summary": "Corner summary in natural Japanese",
    "tags": ["タグ1", "タグ2", "タグ3"],
    "segment": "news"
  }
]

tags rules:
- 3 to 6 tags per corner in Japanese
- Include: topic category (e.g. スポーツ, 天気, 社会, 芸能), location, key person names, organization names, and keywords useful for search
- Short and specific (1-4 characters each preferred)

segment rules:
- Choose exactly one value from: news, weather, sports, feature, ent, live, opening, ending, cm, sponsor, other
- news: ニュース・報道 / weather: 天気予報 / sports: スポーツ / feature: 特集・企画
- ent: エンタメ全般 / live: 中継 / opening: 番組オープニング / ending: 番組エンディング
- cm: CM・広告 / sponsor: 提供クレジット / other: 上記以外
""".strip()

import threading

_WHISPER_MODEL_CACHE: dict[str, object] = {}
_PADDLE_OCR = None
_PADDLE_IMPORT_WARNED = False
_PADDLE_DISABLED = False
_PADDLE_OCR_LOCK = threading.Lock()
_EASY_OCR = None
_EASY_OCR_WARNED = False


def _resolve_whisper_device() -> str:
    configured = settings.whisper_device.lower().strip()
    if configured in {"cpu", "cuda"}:
        return configured
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _resolve_whisper_fp16(device: str) -> bool:
    return device == "cuda"


def _resolve_ocr_device() -> str:
    configured = settings.ocr_use_gpu.lower().strip()
    if configured in {"true", "1", "yes"}:
        return "gpu:0"
    if configured in {"false", "0", "no"}:
        return "cpu"
    try:
        import paddle  # type: ignore

        if bool(getattr(paddle.device, "is_compiled_with_cuda", lambda: False)()):
            return "gpu:0"
    except Exception:
        pass
    return "cpu"


def _dump_gemini_payload(payload: dict) -> Path | None:
    if not settings.gemini_payload_dump:
        return None

    dump_dir = Path(settings.gemini_payload_dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)
    filename = f"gemini_payload_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    target = dump_dir / filename
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _get_whisper_model(model_name: str):
    device = _resolve_whisper_device()
    cache_key = f"{model_name}:{device}"
    model = _WHISPER_MODEL_CACHE.get(cache_key)
    if model is None:
        model = whisper.load_model(model_name, device=device)
        _WHISPER_MODEL_CACHE[cache_key] = model
    return model


def create_grayscale_video(video_path: Path) -> Path:
    grayscale_path = video_path.with_name(f"{video_path.stem}_gray.mp4")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        "format=gray,eq=contrast=1.35:brightness=0.03",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(grayscale_path),
    ]
    try:
        subprocess.run(cmd, check=True)
        return grayscale_path
    except Exception:
        logger.exception("Failed to create grayscale video. Falling back to original video.")
        return video_path


def _transcribe_chunk_job(
    chunk_path: str,
    chunk_index: int,
    chunk_sec: int,
    model_name: str,
    language: str | None,
    beam_size: int,
    best_of: int,
    temperature: float,
) -> dict:
    model = _get_whisper_model(model_name)
    part = model.transcribe(
        chunk_path,
        language=language,
        beam_size=beam_size,
        best_of=best_of,
        temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        fp16=_resolve_whisper_fp16(_resolve_whisper_device()),
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
    )
    return {
        "index": chunk_index,
        "offset": float(chunk_index * chunk_sec),
        "text": (part.get("text") or "").strip(),
        "segments": part.get("segments", []),
        "language": part.get("language"),
    }


def _transcribe_chunk_job_from_tuple(args: tuple) -> dict:
    return _transcribe_chunk_job(*args)


def transcribe_video(video_path: Path) -> dict:
    model = _get_whisper_model(settings.whisper_model)
    language = None if settings.whisper_language.lower() == "auto" else settings.whisper_language
    chunk_sec = int(settings.whisper_chunk_sec)
    workers = int(settings.whisper_parallel_workers)

    if chunk_sec <= 0:
        return model.transcribe(
            str(video_path),
            language=language,
            beam_size=settings.whisper_beam_size,
            best_of=settings.whisper_best_of,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            fp16=_resolve_whisper_fp16(_resolve_whisper_device()),
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
        )

    with tempfile.TemporaryDirectory(prefix="whisper_chunks_", dir="A:\\tmp") as tmp_dir:
        tmp_path = Path(tmp_dir)
        pattern = tmp_path / "chunk_%05d.wav"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_sec),
            "-reset_timestamps",
            "1",
            str(pattern),
        ]
        try:
            subprocess.run(cmd, check=True)
        except Exception:
            return model.transcribe(
                str(video_path),
                language=language,
                beam_size=settings.whisper_beam_size,
                best_of=settings.whisper_best_of,
                temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                fp16=_resolve_whisper_fp16(_resolve_whisper_device()),
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                compression_ratio_threshold=2.4,
            )

        chunk_files = sorted(tmp_path.glob("chunk_*.wav"))
        if len(chunk_files) <= 1:
            return model.transcribe(
                str(video_path),
                language=language,
                beam_size=settings.whisper_beam_size,
                best_of=settings.whisper_best_of,
                temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                fp16=_resolve_whisper_fp16(_resolve_whisper_device()),
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                compression_ratio_threshold=2.4,
            )

        max_workers = max(1, min(workers, len(chunk_files)))
        jobs = [
            (
                str(chunk_file),
                idx,
                chunk_sec,
                settings.whisper_model,
                language,
                settings.whisper_beam_size,
                settings.whisper_best_of,
                settings.whisper_temperature,
            )
            for idx, chunk_file in enumerate(chunk_files)
        ]

        if max_workers == 1:
            parts = [
                _transcribe_chunk_job(*job)
                for job in tqdm(jobs, desc="Whisper", unit="chunk")
            ]
        else:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                parts = list(tqdm(
                    executor.map(_transcribe_chunk_job_from_tuple, jobs),
                    total=len(jobs), desc="Whisper", unit="chunk",
                ))

        parts.sort(key=lambda p: p["index"])

        merged_segments: list[dict] = []
        merged_text_parts: list[str] = []
        merged_language = None
        seg_id = 0

        for part in parts:
            offset = float(part["offset"])
            if not merged_language:
                merged_language = part.get("language")

            text = (part.get("text") or "").strip()
            if text:
                merged_text_parts.append(text)

            for seg in part.get("segments", []):
                start = float(seg.get("start", 0.0)) + offset
                end = float(seg.get("end", start)) + offset
                if end <= start:
                    continue
                copied = dict(seg)
                copied["id"] = seg_id
                copied["start"] = start
                copied["end"] = end
                merged_segments.append(copied)
                seg_id += 1

        return {
            "text": " ".join(merged_text_parts).strip(),
            "segments": merged_segments,
            "language": merged_language,
        }


def _apply_custom_vocabulary(text: str) -> str:
    """Replace Whisper transcription errors using user-registered vocabulary dict.

    ``settings.custom_vocabulary`` should be a JSON object string mapping wrong
    readings to correct surface forms, e.g.::

        {"市家": "シカ", "休憩": "求刑", "機能感染": "はしか感染"}
    """
    raw = settings.custom_vocabulary.strip()
    if not raw:
        return text
    try:
        replacements: dict = json.loads(raw)
    except Exception:
        logger.warning("custom_vocabulary is not valid JSON; skipping replacement")
        return text
    for wrong, correct in replacements.items():
        text = text.replace(str(wrong), str(correct))
    return text


def _segment_rows(transcript: dict) -> list[dict]:
    rows: list[dict] = []
    for seg in transcript.get("segments", []):
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        no_speech_prob = seg.get("no_speech_prob")
        if end <= start:
            continue
        text = _apply_custom_vocabulary((seg.get("text") or "").strip())
        rows.append(
            {
                "start_sec": start,
                "end_sec": end,
                "text": text,
            }
        )

    if not rows:
        return rows
    deduped: list[dict] = [rows[0]]
    for row in rows[1:]:
        if row["text"] == deduped[-1]["text"]:
            deduped[-1]["end_sec"] = row["end_sec"]
        else:
            deduped.append(row)
    return deduped


def _boundary_candidates(rows: list[dict]) -> list[float]:
    if not rows:
        return [0.0]

    candidates = [rows[0]["start_sec"]]
    for prev, cur in zip(rows, rows[1:]):
        gap = cur["start_sec"] - prev["end_sec"]
        if gap >= settings.merge_gap_sec:
            candidates.append(cur["start_sec"])

    candidates.append(rows[-1]["end_sec"])
    return sorted({round(x, 1) for x in candidates})


def _normalize_ocr_text(text: str) -> str:
    text = text.replace("\x0c", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_japanese_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF   # ひらがな・カタカナ
        or 0x4E00 <= code <= 0x9FFF  # 漢字
        or code == 0x3005
        or 0x3000 <= code <= 0x303F  # 「」『』【】、。・ など
    )


def _clean_ocr_token(text: str) -> str:
    text = text.replace("\x0c", " ")
    # \u3000-\u303F = CJK\u8A18\u53F7\u30FB\u53E5\u8AAD\u70B9\uFF08\u300C\u300D\u300E\u300F\u3010\u3011\u3001\u3002\u30FB\u306A\u3069\uFF09
    if settings.ocr_japanese_only:
        text = re.sub(
            r"[^\u3040-\u30FF\u4E00-\u9FFF\u3000-\u303F0-9A-Za-z\s\-_/:\.,!?()]+",
            " ",
            text,
        )
    else:
        text = re.sub(
            r"[^\u3040-\u30FF\u4E00-\u9FFF\u3000-\u303FA-Za-z0-9\s\-_/:\.,!?()]+",
            " ",
            text,
        )
    # Remove isolated 1-2 char lowercase tokens (OCR noise from misread Japanese glyphs)
    text = re.sub(r"(?<![A-Za-z])[a-z]{1,2}(?![A-Za-z])", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _token_quality_ok(text: str) -> bool:
    cleaned = _clean_ocr_token(text)
    if not cleaned:
        return False

    raw_compact = re.sub(r"\s+", "", text)
    cleaned_compact = re.sub(r"\s+", "", cleaned)
    if not raw_compact or not cleaned_compact:
        return False

    valid_ratio = len(cleaned_compact) / max(len(raw_compact), 1)
    if valid_ratio < float(settings.ocr_text_valid_char_ratio):
        return False

    jp_count = sum(1 for ch in cleaned_compact if _is_japanese_char(ch))
    jp_ratio = jp_count / max(len(cleaned_compact), 1)
    if settings.ocr_japanese_only:
        if jp_count < 1:
            return False
        if jp_ratio < max(float(settings.ocr_text_min_japanese_ratio), 0.05):
            return False
    else:
        if jp_ratio < float(settings.ocr_text_min_japanese_ratio):
            if not re.search(r"\b[A-Z]{2,}[A-Z0-9]*\b", cleaned):
                return False

    # Reject if most tokens are very short (noise pattern like "S F oo SS")
    tokens = cleaned.split()
    if len(tokens) >= 4:
        short_tokens = sum(1 for t in tokens if len(t) <= 2)
        if short_tokens / len(tokens) > 0.5:
            return False

    return True


def _paddle_box_wh_ok(points: object) -> bool:
    if not isinstance(points, (list, tuple)) or len(points) < 4:
        return False
    try:
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
    except Exception:
        return False

    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    return (
        width >= float(settings.ocr_paddle_min_box_width)
        and float(settings.ocr_paddle_min_box_height) <= height <= float(settings.ocr_paddle_max_box_height)
    )


def _get_tesseract_languages() -> set[str]:
    cmd = [settings.ocr_tesseract_cmd, "--list-langs"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except OSError:
        return set()

    langs: set[str] = set()
    lines = (result.stdout or "").splitlines()
    for line in lines:
        v = line.strip()
        if not v or v.startswith("List of available languages"):
            continue
        langs.add(v)
    return langs


def _get_paddle_ocr():
    global _PADDLE_OCR, _PADDLE_IMPORT_WARNED, _PADDLE_DISABLED
    if _PADDLE_DISABLED:
        return None
    if _PADDLE_OCR is not None:
        return _PADDLE_OCR
    with _PADDLE_OCR_LOCK:
        if _PADDLE_OCR is not None:
            return _PADDLE_OCR
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except Exception as e:
            if not _PADDLE_IMPORT_WARNED:
                logger.warning("PaddleOCR import failed: %s", e, exc_info=True)
                _PADDLE_IMPORT_WARNED = True
            return None

        model_root = str(Path(__file__).resolve().parent.parent / "paddleocr_models")
        use_gpu = _resolve_ocr_device().startswith("gpu")
        try:
            _PADDLE_OCR = PaddleOCR(
                lang="japan",
                use_angle_cls=False,
                use_gpu=use_gpu,
                show_log=False,
                det_model_dir=str(Path(model_root) / "det"),
                rec_model_dir=str(Path(model_root) / "rec"),
                cls_model_dir=str(Path(model_root) / "cls"),
            )
            logger.info("PaddleOCR 2.x initialized (gpu=%s)", use_gpu)
        except Exception:
            logger.exception("PaddleOCR initialization failed.")
            _PADDLE_OCR = None
            _PADDLE_DISABLED = True
    return _PADDLE_OCR


def _resolve_easy_ocr_gpu() -> bool:
    configured = settings.ocr_use_gpu.lower().strip()
    if configured in {"true", "1", "yes"}:
        return True
    if configured in {"false", "0", "no"}:
        return False
    try:
        import torch  # type: ignore
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _get_easy_ocr():
    global _EASY_OCR, _EASY_OCR_WARNED
    if _EASY_OCR is not None:
        return _EASY_OCR
    try:
        import easyocr  # type: ignore
    except ImportError:
        if not _EASY_OCR_WARNED:
            logger.warning("EasyOCR unavailable. Install with: pip install easyocr")
            _EASY_OCR_WARNED = True
        return None
    try:
        gpu = _resolve_easy_ocr_gpu()
        _EASY_OCR = easyocr.Reader(["ja", "en"], gpu=gpu)
        logger.info("EasyOCR initialized (gpu=%s)", gpu)
    except Exception:
        logger.exception("EasyOCR initialization failed.")
        _EASY_OCR = None
    return _EASY_OCR


def _ocr_regions_easy(image_path: str) -> list[dict]:
    reader = _get_easy_ocr()
    if reader is None:
        return []
    try:
        raw = reader.readtext(image_path)
    except Exception:
        logger.debug("EasyOCR readtext failed", exc_info=True)
        return []

    regions: list[dict] = []
    n_score = n_quality = n_box = n_telop = 0
    for item in raw or []:
        if not item or len(item) < 3:
            continue
        box, text, score = item[0], str(item[1]).strip(), float(item[2])
        if score < float(settings.ocr_paddle_min_score):
            n_score += 1
            continue
        if not _token_quality_ok(text):
            n_quality += 1
            logger.debug("quality_drop score=%.2f text=%r", score, text[:40])
            continue
        if not _paddle_box_wh_ok(box):
            n_box += 1
            continue
        cleaned = _clean_ocr_token(text)
        if not cleaned:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            x, y = min(xs), min(ys)
            width, height = max(xs) - x, max(ys) - y
        except Exception:
            continue
        if not _is_telop_like_region(x, y, width, height):
            n_telop += 1
            continue
        regions.append({"x": x, "y": y, "w": width, "h": height, "text": cleaned, "score": score})

    if raw:
        print(f"[easy_ocr_filter] total={len(raw)} pass={len(regions)} "
              f"drop_score={n_score} drop_quality={n_quality} drop_box={n_box} drop_telop={n_telop}")
    return _merge_same_line_regions(regions)


def _text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(a=a, b=b).ratio()


def _is_telop_like_region(x: float, y: float, width: float, height: float) -> bool:
    if width < float(settings.ocr_paddle_min_box_width):
        return False
    if not (float(settings.ocr_paddle_min_box_height) <= height <= float(settings.ocr_paddle_max_box_height)):
        return False

    # Ignore tiny boxes that are often logos/noise.
    if width * height < 1200:
        return False

    # 単一文字（括弧など）は正方形に近いので縦長でなければ許容する
    if width / max(height, 1.0) < 0.3:
        return False

    return True


def _merge_same_line_regions(regions: list[dict]) -> list[dict]:
    if len(regions) <= 1:
        return regions

    lines: list[list[dict]] = []
    for region in sorted(regions, key=lambda r: (r["y"], r["x"])):
        placed = False
        for line in lines:
            ref = line[-1]
            ref_cy = ref["y"] + ref["h"] / 2
            cur_cy = region["y"] + region["h"] / 2
            same_row = abs(ref_cy - cur_cy) < max(ref["h"], region["h"]) * 0.6
            # 直前ボックスの右端から現在ボックスの左端までのX距離
            x_gap = region["x"] - (ref["x"] + ref["w"])
            close_enough = x_gap < max(ref["h"], region["h"]) * 2.0
            if same_row and close_enough:
                line.append(region)
                placed = True
                break
        if not placed:
            lines.append([region])

    result = []
    for line in lines:
        line.sort(key=lambda r: r["x"])
        text = " ".join(r["text"] for r in line).strip()
        x = min(r["x"] for r in line)
        y = min(r["y"] for r in line)
        w = max(r["x"] + r["w"] for r in line) - x
        h = max(r["h"] for r in line)
        score = max(r["score"] for r in line)
        result.append({"x": x, "y": y, "w": w, "h": h, "text": text, "score": score})
    return result


def _ocr_regions_paddle(image_path: str) -> list[dict]:
    ocr = _get_paddle_ocr()
    if ocr is None:
        return []
    try:
        with _PADDLE_OCR_LOCK:
            result = ocr.ocr(image_path, cls=False)
    except Exception:
        logger.warning("PaddleOCR ocr() failed", exc_info=True)
        return []

    regions: list[dict] = []
    for line in result or []:
        if not line:
            continue
        for item in line:
            if not item or len(item) < 2:
                continue
            box = item[0]
            rec = item[1]
            if not _paddle_box_wh_ok(box):
                continue
            if not isinstance(rec, (list, tuple)) or not rec:
                continue
            text = str(rec[0]).strip()
            score = float(rec[1]) if len(rec) > 1 else 0.0
            if score < float(settings.ocr_paddle_min_score):
                continue
            if not _token_quality_ok(text):
                continue
            cleaned = _clean_ocr_token(text)
            if not cleaned:
                continue
            try:
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                x, y = min(xs), min(ys)
                width, height = max(xs) - x, max(ys) - y
            except Exception:
                continue
            if not _is_telop_like_region(x, y, width, height):
                continue
            regions.append({"x": x, "y": y, "w": width, "h": height, "text": cleaned, "score": score})

    return _merge_same_line_regions(regions)


def _ocr_image_text_tesseract(image_path: str, psm: int = 6) -> str:
    cmd = [
        settings.ocr_tesseract_cmd,
        image_path,
        "stdout",
        "-l",
        settings.ocr_languages,
        "--psm",
        str(psm),
        "-c",
        "preserve_interword_spaces=1",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return _normalize_ocr_text(result.stdout)


def _ocr_frame_regions(frame_image_path: str) -> list[dict]:
    engine = settings.ocr_engine.lower().strip()
    if engine == "easyocr":
        return _ocr_regions_easy(frame_image_path)
    if engine == "paddleocr":
        regions = _ocr_regions_paddle(frame_image_path)
        if regions:
            return regions
        text = _ocr_image_text_tesseract(frame_image_path, psm=6)
        cleaned = _clean_ocr_token(text)
        if len(cleaned) < int(settings.ocr_min_chars):
            return []
        return [{"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0, "text": cleaned, "score": 1.0}]

    text = _ocr_image_text_tesseract(frame_image_path, psm=6)
    cleaned = _clean_ocr_token(text)
    if len(cleaned) < int(settings.ocr_min_chars):
        return []
    return [{"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0, "text": cleaned, "score": 1.0}]


def _region_match(prev: dict, cur: dict, interval: float) -> bool:
    if cur["start_sec"] - prev["end_sec"] > (interval * 1.5):
        return False

    sim = _text_similarity(prev["text"], cur["text"])
    if sim < float(settings.ocr_region_match_text_sim):
        return False

    if abs(prev["x"] - cur["x"]) > float(settings.ocr_region_match_x_px):
        return False
    if abs(prev["y"] - cur["y"]) > float(settings.ocr_region_match_y_px):
        return False

    return True


def _merge_region_rows(raw_rows: list[dict], interval: float) -> list[dict]:
    if not raw_rows:
        return []

    merged: list[dict] = []
    for cur in raw_rows:
        matched = False
        for prev in reversed(merged):
            if _region_match(prev, cur, interval):
                prev["end_sec"] = cur["end_sec"]
                prev["hits"] += 1
                if len(cur["text"]) > len(prev["text"]):
                    prev["text"] = cur["text"]
                matched = True
                break
            if cur["start_sec"] - prev["end_sec"] > (interval * 2.0):
                break

        if not matched:
            merged.append(dict(cur))

    min_persist = max(1, int(settings.ocr_min_persist_frames))
    return [
        {
            "start_sec": row["start_sec"],
            "end_sec": row["end_sec"],
            "text": row["text"],
        }
        for row in merged
        if row["hits"] >= min_persist and len(row["text"]) >= int(settings.ocr_min_chars)
    ]


def _fallback_rows_from_raw_regions(raw_rows: list[dict], interval: float) -> list[dict]:
    grouped: dict[tuple[float, float], list[str]] = {}
    for row in raw_rows:
        key = (float(row["start_sec"]), float(row["end_sec"]))
        grouped.setdefault(key, []).append(str(row["text"]))

    fallback_rows: list[dict] = []
    for (start, end), texts in sorted(grouped.items(), key=lambda v: v[0][0]):
        deduped: list[str] = []
        seen: set[str] = set()
        for t in texts:
            if t in seen:
                continue
            seen.add(t)
            deduped.append(t)
        merged_text = _normalize_ocr_text(" ".join(deduped[:6]))
        if len(merged_text) < int(settings.ocr_min_chars):
            continue
        fallback_rows.append(
            {
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "text": merged_text,
            }
        )
    return fallback_rows


def _extract_ocr_rows(video_path: Path) -> list[dict]:
    if not settings.ocr_enabled:
        return []

    interval = float(settings.ocr_interval_sec)
    if interval <= 0:
        return []

    engine = settings.ocr_engine.lower().strip()
    if engine == "tesseract":
        if shutil.which(settings.ocr_tesseract_cmd) is None:
            logger.warning("OCR skipped: tesseract command not found (%s)", settings.ocr_tesseract_cmd)
            return []
        required_langs = [v.strip() for v in settings.ocr_languages.split("+") if v.strip()]
        available_langs = _get_tesseract_languages()
        missing_langs = [v for v in required_langs if v not in available_langs]
        if missing_langs:
            logger.warning(
                "OCR skipped: missing traineddata for languages=%s, available=%s",
                ",".join(missing_langs),
                ",".join(sorted(available_langs)) or "(none)",
            )
            return []

    with tempfile.TemporaryDirectory(prefix="ocr_frames_", dir="A:\\tmp") as tmp_dir:
        tmp_path = Path(tmp_dir)
        frame_pattern = tmp_path / "frame_%06d.png"
        fps = 1.0 / interval
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps},format=gray,scale=iw*1.5:ih*1.5,eq=contrast=1.4:brightness=0.02",
            str(frame_pattern),
        ]

        try:
            with tqdm(desc="ffmpeg (フレーム抽出)", unit="s", bar_format="{desc}: {elapsed} 経過") as _:
                subprocess.run(ffmpeg_cmd, check=True)
        except Exception:
            return []

        frame_files = sorted(tmp_path.glob("frame_*.png"))
        frame_count = len(frame_files)
        if frame_count == 0:
            return []

        max_workers = max(1, min(int(settings.ocr_parallel_workers), frame_count))
        frame_jobs = [str(frame_file) for frame_file in frame_files]
        if max_workers == 1:
            frame_regions = [
                _ocr_frame_regions(p)
                for p in tqdm(frame_jobs, desc="OCR", unit="frame")
            ]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                frame_regions = list(tqdm(
                    executor.map(_ocr_frame_regions, frame_jobs),
                    total=frame_count, desc="OCR", unit="frame",
                ))

        raw_rows: list[dict] = []
        for idx, regions in enumerate(frame_regions):
            if not regions:
                continue
            start = round(idx * interval, 3)
            end = round(start + interval, 3)
            for r in regions:
                raw_rows.append(
                    {
                        "start_sec": start,
                        "end_sec": end,
                        "text": _normalize_ocr_text(r["text"]),
                        "x": float(r["x"]),
                        "y": float(r["y"]),
                        "w": float(r["w"]),
                        "h": float(r["h"]),
                        "hits": 1,
                    }
                )

        if not raw_rows:
            return []

        raw_rows.sort(key=lambda r: (r["start_sec"], r["y"], r["x"]))
        merged = _merge_region_rows(raw_rows, interval)
        if not merged:
            merged = _fallback_rows_from_raw_regions(raw_rows, interval)

        logger.info("OCR extracted rows: raw=%d merged=%d", len(raw_rows), len(merged))
        print(f"[ocr_rows] raw={len(raw_rows)} merged={len(merged)}")
        return merged


def _fallback_segments(transcript: dict) -> list[dict]:
    text = transcript.get("text", "").strip()
    duration = 0.0
    segments = transcript.get("segments", [])
    if segments:
        duration = float(segments[-1].get("end", 0.0))

    return [
        {
            "start_sec": 0.0,
            "end_sec": max(duration, 1.0),
            "title": "Overall",
            "summary": text[:400] if text else "Transcription result could not be retrieved.",
        }
    ]


def _normalize_corners(corners: list[dict], rows: list[dict]) -> list[dict]:
    if not rows:
        return corners

    min_t = rows[0]["start_sec"]
    max_t = rows[-1]["end_sec"]

    cleaned: list[dict] = []
    for c in corners:
        try:
            start = max(min_t, float(c["start_sec"]))
            end = min(max_t, float(c["end_sec"]))
            if end <= start:
                continue
            cleaned.append(
                {
                    "start_sec": start,
                    "end_sec": end,
                    "title": str(c.get("title", "Corner")).strip() or "Corner",
                    "summary": str(c.get("summary", "")).strip() or "No summary",
                    "tags": [str(t) for t in c.get("tags", []) if t],
                    "segment": str(c.get("segment", "other")).strip() or "other",
                }
            )
        except (TypeError, ValueError, KeyError):
            continue

    cleaned.sort(key=lambda x: x["start_sec"])
    if not cleaned:
        return _fallback_segments({"segments": rows, "text": ""})

    merged: list[dict] = [cleaned[0]]
    for cur in cleaned[1:]:
        prev = merged[-1]
        if cur["start_sec"] - prev["end_sec"] <= settings.merge_gap_sec:
            if prev["end_sec"] - prev["start_sec"] < settings.min_corner_sec:
                prev["end_sec"] = max(prev["end_sec"], cur["end_sec"])
                prev["summary"] = f"{prev['summary']} {cur['summary']}".strip()
                continue
        merged.append(cur)

    # 前のコーナーを次の開始時刻まで延ばしてギャップを埋める
    for i in range(len(merged) - 1):
        if merged[i + 1]["start_sec"] > merged[i]["end_sec"]:
            merged[i]["end_sec"] = merged[i + 1]["start_sec"]

    merged[0]["start_sec"] = min_t
    merged[-1]["end_sec"] = max_t
    return merged


_VISION_PROMPT = """
You are a news video corner segmentation assistant.
You have access to both speech transcription and sampled video frames.

Input:
- audio_rows: Whisper speech transcription rows with start_sec/end_sec/text (JSON below)
- boundary_candidates_sec: candidate boundary timestamps based on audio gaps
- Video frames are provided as images after the JSON, each preceded by a timestamp label like [Frame at 10.0s]

Use BOTH the audio content AND what you see in the frames to:
- Read on-screen text (テロップ, graphics, scores, names) directly from frames
- Detect visual topic/scene changes (studio vs. footage, graphics overlays, etc.)
- Identify corner boundaries more accurately than audio alone

Rules:
- One corner should contain one topic.
- Merge ALL weather forecast content into one single corner titled "天気予報". Do not split weather.
- Separate unrelated topics into separate corners.
- In news programs, treat each individual news story as a separate corner, even if short.
- Studio commentary or reactions about the same news story should be merged into the same corner.
- Sponsored segments (プレゼント, CM, お知らせ) should be their own corner, even if very short.
- Keep boundaries near candidate timestamps when possible.
- Prioritize Japanese text visible in frames; ignore logos, sponsor names, and decorative graphics.
- Whisper may contain recognition errors; use on-screen text from frames to correct titles and summaries.

Return only JSON array:
[
  {
    "start_sec": 0.0,
    "end_sec": 123.4,
    "title": "Corner title in Japanese",
    "summary": "Corner summary in natural Japanese",
    "tags": ["タグ1", "タグ2", "タグ3"],
    "segment": "news"
  }
]

tags rules:
- 3 to 6 tags per corner in Japanese
- Include: topic category (e.g. スポーツ, 天気, 社会, 芸能), location, key person names, organization names, and keywords useful for search
- Short and specific (1-4 characters each preferred)

segment rules:
- Choose exactly one value from: news, weather, sports, feature, ent, live, opening, ending, cm, sponsor, other
- news: ニュース・報道 / weather: 天気予報 / sports: スポーツ / feature: 特集・企画
- ent: エンタメ全般 / live: 中継 / opening: 番組オープニング / ending: 番組エンディング
- cm: CM・広告 / sponsor: 提供クレジット / other: 上記以外
""".strip()


def _extract_keyframes(video_path: Path) -> list[dict]:
    """Extract one frame every vision_frame_interval_sec up to vision_max_frames."""
    interval = settings.vision_frame_interval_sec
    max_frames = settings.vision_max_frames

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        duration = 0.0

    if duration <= 0:
        return []

    timestamps = []
    t = 0.0
    while t <= duration and len(timestamps) < max_frames:
        timestamps.append(round(t, 1))
        t += interval

    frames: list[dict] = []
    with tempfile.TemporaryDirectory(dir="A:\\tmp") as tmp:
        tmp_path = Path(tmp)
        for ts in timestamps:
            out = tmp_path / f"frame_{ts:.1f}.jpg"
            ret = subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", str(ts),
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-q:v", "3",
                    str(out),
                ],
                capture_output=True,
            )
            if ret.returncode == 0 and out.exists():
                frames.append({"time_sec": ts, "image": Image.open(out).copy()})

    logger.info("Extracted %d keyframes from %s", len(frames), video_path.name)
    return frames


def segment_corners_vision(transcript: dict, video_path: Path) -> dict:
    """Segment corners using Whisper transcript + Gemini Vision on keyframes."""
    audio_rows = _segment_rows(transcript)
    keyframes = _extract_keyframes(video_path)
    print(f"[segment_corners_vision] audio_rows={len(audio_rows)} keyframes={len(keyframes)}")

    if not settings.gemini_api_key:
        return {
            "corners": _fallback_segments(transcript),
            "audio_rows": audio_rows,
            "keyframe_count": len(keyframes),
        }

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)

    payload_text = {
        "audio_rows": audio_rows,
        "boundary_candidates_sec": _boundary_candidates(audio_rows),
    }
    prompt_text = (
        f"{_VISION_PROMPT}\n\n"
        f"Audio data (JSON):\n{json.dumps(payload_text, ensure_ascii=False)}\n\n"
        "Video frames follow (each labeled with its timestamp):"
    )

    content: list = [prompt_text]
    for frame in keyframes:
        content.append(f"\n[Frame at {frame['time_sec']:.1f}s]")
        content.append(frame["image"])

    try:
        response = model.generate_content(content)
    except Exception:
        logger.exception("Gemini Vision request failed: keyframes=%d", len(keyframes))
        return {
            "corners": _fallback_segments(transcript),
            "audio_rows": audio_rows,
            "keyframe_count": len(keyframes),
        }

    raw = (response.text or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.removeprefix("```json").removesuffix("```").strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.error("Gemini Vision JSON parse failed. raw_preview=%r", raw[:500])
            return {
                "corners": _fallback_segments(transcript),
                "audio_rows": audio_rows,
                "keyframe_count": len(keyframes),
            }

    normalized = _normalize_corners(parsed, audio_rows)
    if not normalized:
        normalized = _fallback_segments(transcript)

    return {
        "corners": normalized,
        "audio_rows": audio_rows,
        "keyframe_count": len(keyframes),
    }


def segment_corners(video_path: Path) -> dict:
    """Whisper文字起こしとOCRを並列実行してからGeminiでコーナー分類する。"""
    with ThreadPoolExecutor(max_workers=2) as ex:
        audio_future = ex.submit(transcribe_video, video_path)
        ocr_future = ex.submit(_extract_ocr_rows, video_path)
        transcript = audio_future.result()
        ocr_rows = ocr_future.result()
    audio_rows = _segment_rows(transcript)
    print(f"[segment_corners] audio_rows={len(audio_rows)} ocr_rows={len(ocr_rows)}")

    if not settings.gemini_api_key:
        return {
            "corners": _fallback_segments(transcript),
            "audio_rows": audio_rows,
            "ocr_rows": ocr_rows,
        }

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)

    payload = {
        "audio_rows": audio_rows,
        "ocr_rows": ocr_rows,
        "boundary_candidates_sec": _boundary_candidates(audio_rows),
    }
    dumped = _dump_gemini_payload(payload)
    if dumped:
        print(f"[segment_corners] payload_saved={dumped}")
    prompt = f"{_PROMPT}\n\nInput data (JSON):\n{json.dumps(payload, ensure_ascii=False)}"
    try:
        response = model.generate_content(prompt)
    except Exception:
        logger.exception(
            "Gemini request failed: audio_rows=%d ocr_rows=%d",
            len(audio_rows),
            len(ocr_rows),
        )
        return {
            "corners": _fallback_segments(transcript),
            "audio_rows": audio_rows,
            "ocr_rows": ocr_rows,
            "gemini_usage": None,
        }

    usage = getattr(response, "usage_metadata", None)
    gemini_usage = None
    if usage is not None:
        gemini_usage = {
            "input_tokens": getattr(usage, "prompt_token_count", 0),
            "output_tokens": getattr(usage, "candidates_token_count", 0),
            "total_tokens": getattr(usage, "total_token_count", 0),
        }
        print(f"[gemini_usage] {gemini_usage}")

    raw = (response.text or "").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.removeprefix("```json").removesuffix("```").strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.error("Gemini JSON parse failed. raw_preview=%r", raw[:500])
            return {
                "corners": _fallback_segments(transcript),
                "audio_rows": audio_rows,
                "ocr_rows": ocr_rows,
                "gemini_usage": gemini_usage,
            }

    normalized = _normalize_corners(parsed, audio_rows)
    if not normalized:
        normalized = _fallback_segments(transcript)

    return {
        "corners": normalized,
        "audio_rows": audio_rows,
        "ocr_rows": ocr_rows,
        "gemini_usage": gemini_usage,
    }
