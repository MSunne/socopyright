from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import admin_api, admin_config_api, job_api, user_store, worker
from .auth import create_access_token, verify_password
from .db import init_db
from .schemas import LoginReq, TokenResp
from .screenshot.capture import shutdown_browser

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("./data").mkdir(parents=True, exist_ok=True)
    Path("./data/generated").mkdir(parents=True, exist_ok=True)
    await init_db()
    # 启动串行 worker（单消费者协程）
    worker.start_worker()
    # 恢复重启前未完成的任务（按创建时间顺序重新入队）
    await worker.resume_pending()
    yield
    # 关闭时释放 Playwright
    await shutdown_browser()


app = FastAPI(title="软著申请生成系统", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/auth/login", response_model=TokenResp, tags=["auth"])
async def login(body: LoginReq) -> TokenResp:
    user = await user_store.get_by_username(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not user["is_active"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user inactive")
    return TokenResp(access_token=create_access_token(user["username"]))


app.include_router(admin_api.router)
app.include_router(admin_config_api.router)
app.include_router(job_api.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "admin.html"))
