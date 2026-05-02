from datetime import date, datetime

from pydantic import BaseModel, Field


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


class JobCreate(BaseModel):
    company_name: str = Field(min_length=2, max_length=255)
    uscc: str = Field(min_length=18, max_length=18)
    established_date: date
    quantity: int = Field(ge=1, le=50)
    keywords: list[str] = Field(default_factory=list)
    language: str | None = None
    template: str = Field(default="basic", pattern=r"^(basic|rich)$")


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
