"""API 进程入口（docker-compose 运行：python -m app.main）。"""

import logging

import uvicorn
from fastapi import FastAPI

from app.admin.routes import router as admin_router
from app.config import get_settings
from app.database.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(title="ArchiveBOT Admin", version="0.1.0")
app.include_router(admin_router)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.web_admin_host,
        port=settings.web_admin_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
