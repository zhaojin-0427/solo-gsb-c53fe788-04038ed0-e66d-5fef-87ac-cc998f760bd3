"""请求体模型（Pydantic v2）。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _strip(v: str) -> str:
    return v.strip() if isinstance(v, str) else v


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class BatchIn(BaseModel):
    batch_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)
    temp_min: float
    temp_max: float

    _clean = field_validator("batch_code", "name", "description", mode="before")(_strip)

    @field_validator("temp_max")
    @classmethod
    def _range_ok(cls, v, info):
        if "temp_min" in info.data and v <= info.data["temp_min"]:
            raise ValueError("temp_max 必须大于 temp_min")
        return v


class SamplesIn(BaseModel):
    barcodes: list[str] = Field(min_length=1, max_length=2000)

    @field_validator("barcodes")
    @classmethod
    def _non_empty(cls, v):
        cleaned = [b.strip() for b in v if b and b.strip()]
        if not cleaned:
            raise ValueError("条码列表不能为空")
        for b in cleaned:
            if len(b) > 128:
                raise ValueError(f"条码过长: {b[:32]}...")
        return cleaned


class HandoverIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=128)
    from_holder: str = Field(min_length=1, max_length=64)
    to_holder: str = Field(min_length=1, max_length=64)
    location: str = Field(min_length=1, max_length=128)
    scanned_at: str | None = Field(default=None, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)

    _clean = field_validator("barcode", "from_holder", "to_holder", "location", "idempotency_key", mode="before")(_strip)

    @field_validator("to_holder")
    @classmethod
    def _not_self(cls, v, info):
        if "from_holder" in info.data and v == info.data["from_holder"]:
            raise ValueError("接收人不能与当前持有人相同")
        return v


class TempReadingIn(BaseModel):
    barcode: str | None = Field(default=None, max_length=128)
    temp: float
    recorded_at: str | None = Field(default=None, max_length=64)

    _clean = field_validator("barcode", mode="before")(_strip)


class TempImportIn(BaseModel):
    batch_code: str = Field(min_length=1, max_length=64)
    import_id: str = Field(min_length=8, max_length=128)
    readings: list[TempReadingIn] = Field(min_length=1, max_length=5000)

    _clean = field_validator("batch_code", "import_id", mode="before")(_strip)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=2, max_length=500)

    _clean = field_validator("reason", mode="before")(_strip)
