"""请求/响应模型。"""
from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field, field_validator


class HarvestRequest(BaseModel):
    until_date: str = Field(
        ...,
        alias="until",
        description="采集截止日（含当天），格式 YYYY-MM-DD",
        examples=["2026-10-15"],
    )
    from_date: str | None = Field(
        default=None,
        alias="from",
        description=(
            "起始日（含当天），首次采集另填；之后留空则自动从上次成功"
            "采集的截止日续采"
        ),
        examples=["2026-10-01"],
    )

    model_config = {"populate_by_name": True}


class HarvestResponse(BaseModel):
    status: str = Field(..., description="success / failed")
    from_date: str = Field(..., description="实际使用的起始日（含）")
    until_date: str = Field(..., description="截止日（含）")
    pages: int = Field(..., description="本次成功拉取的页数")
    fetched: int = Field(..., description="拉取到的记录总数（含删除标记）")
    inserted: int
    updated: int
    withdrawn: int = Field(..., description="因删除标记撤下的记录数")
    ignored: int = Field(
        0, description="按已登记对照忽略的旧号记录数（含旧号删除标记）"
    )
    retries: int = Field(..., description="令牌失效后按原区间重抓的次数（0 或 1）")
    run_id: int
    note: str | None = None


class ProgressResponse(BaseModel):
    last_until: str | None = Field(
        None, description="上次成功采集的截止日（含）；无记录表示尚未采集"
    )
    last_harvest_at: str | None = None


class BibRecordOut(BaseModel):
    identifier: str
    datestamp: str
    status: str
    title: str | None
    creator: str | None
    date: str | None
    identifier_dc: str | None
    updated_at: str


class RunOut(BaseModel):
    id: int
    started_at: str
    finished_at: str | None
    from_date: str
    until_date: str
    status: str
    pages: int
    fetched: int
    inserted: int
    updated: int
    withdrawn: int
    retries: int
    error: str | None


class IdentifierPairIn(BaseModel):
    """一组新旧标识对照。"""

    old_identifier: str = Field(
        ...,
        validation_alias=AliasChoices(
            "old_identifier", "old", "old_id", "oldIdentifier"
        ),
        description="原书目标识，登记时必须在架",
        examples=["oai:sample.lib:rec-001"],
    )
    new_identifier: str = Field(
        ...,
        validation_alias=AliasChoices(
            "new_identifier", "new", "new_id", "newIdentifier"
        ),
        description="成员馆新系统标识，登记时不得已入库",
        examples=["oai:sample2.lib:rec-001"],
    )

    model_config = {"populate_by_name": True}

    @field_validator("old_identifier", "new_identifier")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("标识不能为空")
        return value


class IdentifierMappingRequest(BaseModel):
    mappings: list[IdentifierPairIn] = Field(
        ...,
        validation_alias=AliasChoices("mappings", "pairs", "changes"),
        min_length=1,
        description="整批新旧标识对照",
    )

    model_config = {"populate_by_name": True}


class IdentifierMappingOut(BaseModel):
    old_identifier: str
    new_identifier: str
    registered_at: str


class IdentifierMappingResponse(BaseModel):
    status: str = "success"
    registered: int
    mappings: list[IdentifierMappingOut]
