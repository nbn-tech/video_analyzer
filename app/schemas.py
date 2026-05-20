from pydantic import BaseModel, Field


class TextRowResponse(BaseModel):
    start_sec: float
    end_sec: float
    text: str


class CornerResponse(BaseModel):
    start_sec: float
    end_sec: float
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)


class UploadResponse(BaseModel):
    video_id: int
    filename: str
    processed_filename: str | None = None
    corners: list[CornerResponse]
    audio_rows: list[TextRowResponse] = Field(default_factory=list)
    ocr_rows: list[TextRowResponse] = Field(default_factory=list)
    mode: str = "ocr"
    keyframe_count: int = 0
