import re
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.region import validate_id_card, validate_uscc


class LoginReq(BaseModel):
    username: str
    password: str


class TokenResp(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class UserUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=6, max_length=128)
    is_active: bool | None = None


class UserOut(BaseModel):
    id: int
    username: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


_FULLWIDTH_TR = str.maketrans({
    "（": "(", "）": ")", "／": "/", "－": "-",
    "　": " ",  # 全角空格
})
_MULTI_WS = re.compile(r"\s+")


class JobCreate(BaseModel):
    # owner_kind=company：company_name + 18 位 USCC
    # owner_kind=individual：personal_name + 18 位身份证号（同字段 company_name / uscc 复用以最小改动）
    owner_kind: Literal["company", "individual"] = "company"
    company_name: str = Field(min_length=2, max_length=255)
    uscc: str = Field(min_length=18, max_length=18)
    established_date: date
    quantity: int = Field(ge=1, le=50)
    keywords: list[str] = Field(default_factory=list)
    language: str | None = None
    template: str = Field(default="basic", pattern=r"^(basic|rich)$")

    @field_validator("company_name")
    @classmethod
    def _norm_company_name(cls, v: str) -> str:
        # 全角符号转半角 + strip + 折叠多空格
        v = v.translate(_FULLWIDTH_TR).strip()
        v = _MULTI_WS.sub(" ", v)
        return v

    @model_validator(mode="after")
    def _check_owner(self) -> "JobCreate":
        if self.owner_kind == "company":
            if len(self.company_name) < 4:
                raise ValueError("公司名称过短（至少 4 字）")
            ok, reason = validate_uscc(self.uscc)
            if not ok:
                raise ValueError(f"统一社会信用代码不合法：{reason}")
            # 规范化大写
            object.__setattr__(self, "uscc", self.uscc.strip().upper())
        else:  # individual
            if len(self.company_name) < 2:
                raise ValueError("姓名过短（至少 2 字）")
            ok, reason = validate_id_card(self.uscc)
            if not ok:
                raise ValueError(f"身份证号不合法：{reason}")
            object.__setattr__(self, "uscc", self.uscc.strip().upper())
        return self

    @field_validator("established_date")
    @classmethod
    def _check_established(cls, v: date) -> date:
        today = date.today()
        if v >= today:
            raise ValueError("日期必须早于今天")
        if v < date(1900, 1, 1):
            raise ValueError("日期不应早于 1900-01-01")
        return v


class JobFileOut(BaseModel):
    id: int
    idx: int
    software_name: str
    status: str
    progress: int
    error: str | None

    class Config:
        from_attributes = True


class JobOut(BaseModel):
    id: str
    owner_kind: str = "company"
    company_name: str
    uscc: str
    established_date: date
    quantity: int
    keywords: list[str]
    language: str | None
    template: str
    status: str
    progress: int
    error: str | None
    created_at: datetime
    started_at: datetime | None = None  # 首次进入 running 的时刻；前端"耗时"用 finished_at - started_at 算
    finished_at: datetime | None

    class Config:
        from_attributes = True


class JobDetailOut(JobOut):
    files: list[JobFileOut] = []


class JobListResp(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    page_size: int
