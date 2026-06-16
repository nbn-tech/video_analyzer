from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    whisper_model: str = "small"
    whisper_chunk_sec: int = 60
    whisper_parallel_workers: int = 3
    whisper_language: str = "auto"
    whisper_device: str = "auto"
    whisper_beam_size: int = 5
    whisper_best_of: int = 5
    whisper_temperature: float = 0.0
    ocr_enabled: bool = True
    ocr_engine: str = "paddleocr"
    ocr_interval_sec: float = 1.0
    ocr_parallel_workers: int = 4
    ocr_min_chars: int = 8
    ocr_tesseract_cmd: str = "tesseract"
    ocr_languages: str = "jpn+eng"
    ocr_paddle_min_score: float = 0.75
    ocr_paddle_min_box_width: int = 60
    ocr_paddle_min_box_height: int = 16
    ocr_paddle_max_box_height: int = 220
    ocr_text_valid_char_ratio: float = 0.55
    ocr_text_min_japanese_ratio: float = 0.2
    ocr_japanese_only: bool = True
    ocr_min_persist_frames: int = 1
    ocr_region_match_text_sim: float = 0.82
    ocr_region_match_x_px: int = 120
    ocr_region_match_y_px: int = 80
    ocr_use_gpu: str = "auto"
    gemini_payload_dump: bool = True
    gemini_payload_dump_dir: str = "app/data/debug_payloads"

    min_corner_sec: float = 8.0
    merge_gap_sec: float = 2.0

    # JSON dict of Whisper error corrections, e.g. {"市家": "シカ", "休憩": "求刑"}
    custom_vocabulary: str = ""

    vision_frame_interval_sec: float = 10.0
    vision_max_frames: int = 50

    paddle_pdx_home: str = ""

    database_url: str = "sqlite:///./app/data/video_analyzer.db"

    # AWS
    aws_region: str = "ap-northeast-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    s3_bucket: str = ""
    s3_input_prefix: str = "videos/"
    s3_output_prefix: str = "results/"
    sqs_queue_url: str = ""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
    )


settings = Settings()
