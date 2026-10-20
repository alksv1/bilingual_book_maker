"""Main FastAPI application entrypoint for bilingual_book_maker."""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from server import config
from server.database import init_db, ensure_directories
from server.schemas import ErrorResponse
from server.routers import auth_router, tasks_router, settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("server.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager to initialize directories and SQLite database."""
    logger.info("Starting bilingual_book_maker web service...")
    ensure_directories()
    await init_db()
    logger.info("Persistent directories and SQLite schema verified.")
    yield
    logger.info("Shutting down bilingual_book_maker web service...")


app = FastAPI(
    title="Bilingual Book Maker Web API",
    description="HTTP API & Web Service for automated bilingual book translation.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Global Exception Handlers ensuring unified ErrorResponse format
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            detail=str(exc.detail),
            code=getattr(exc, "code", "HTTP_ERROR"),
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            detail=str(exc),
            code="VALIDATION_ERROR",
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled server exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            detail="Internal Server Error",
            code="INTERNAL_SERVER_ERROR",
        ).model_dump(),
    )


# Mount API routers
app.include_router(auth_router)
app.include_router(tasks_router)
app.include_router(settings_router)


# Health check endpoint
@app.get(
    "/api/health",
    summary="Service healthcheck",
    tags=["system"],
)
async def health_check():
    return {
        "status": "ok",
        "version": "1.0.0",
        "protected_mode": config.is_protected_mode(),
    }


# Static file serving & Frontend SPA fallback
if config.STATIC_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=config.STATIC_DIR, check_dir=False),
        name="static",
    )


@app.get(
    "/",
    summary="Frontend root or API index",
    include_in_schema=False,
)
async def root():
    index_file = config.STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return {
        "message": "Bilingual Book Maker Web API is running.",
        "documentation": "/docs",
        "health": "/api/health",
    }
