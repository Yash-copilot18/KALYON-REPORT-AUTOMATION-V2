# app/main.py

import logging
import sys
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("GE Solar Monitoring API — Starting up")
    logger.info("=" * 60)

    from app.database.base import import_all_models, Base
    from app.database.session import verify_database_connection, engine

    import_all_models()

    connected = verify_database_connection()
    if not connected:
        logger.critical("Cannot connect to SQL Server. Shutting down.")
        sys.exit(1)

    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables verified / created successfully.")
    except Exception as e:
        logger.critical(f"Failed to create database tables: {e}")
        sys.exit(1)

    # Warm the DB schema cache (single query) so report tag-validation runs fully
    # in-memory — no per-request INFORMATION_SCHEMA lookups.
    from app.services import schema_cache
    from app.database.session import SessionLocal
    try:
        with SessionLocal() as _db:
            schema_cache.load_all(_db)
    except Exception as e:
        logger.warning("Schema cache warm-up failed (will lazy-load per table): %s", e)

    # Validate SMTP configuration (non-fatal — email sending degrades gracefully).
    from app.services import email_service
    email_service.log_startup_status()

    logger.info("Application startup complete. Ready to accept requests.")
    yield

    from app.database.session import engine as eng
    logger.info("Shutting down — disposing database connection pool.")
    eng.dispose()
    logger.info("Shutdown complete.")


# ── App Instance ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="GE Solar Monitoring API",
    description=(
        "Production-ready FastAPI backend for GE Vernova Solar Power Plant "
        "Monitoring and Report Automation System.\n\n"
        "Connected to: **SERVICESCADA32\\SQLEXPRESS** · "
        "Database: **Kalyan** · Auth: **Windows Authentication**"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ── CORS ──────────────────────────────────────────────────────────────────────
cors_origins_raw = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000"
)
cors_origins = [o.strip() for o in cors_origins_raw.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "Content-Disposition"],
)


# ── Exception Handlers ────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for error in exc.errors():
        errors.append({
            "field":   " -> ".join(str(loc) for loc in error["loc"]),
            "message": error["msg"],
            "type":    error["type"],
        })
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation error", "errors": errors},
    )


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.error(f"Unhandled SQLAlchemy error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "A database error occurred. Please try again."},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# ── Routers ───────────────────────────────────────────────────────────────────
from app.routers import (
    health,
    equipment,
    reports,
    dashboard,
    analytics,
    equipment_live,
    reports_v2,
    scheduled,
    tracker,
    isolator,
)

app.include_router(health.router)
app.include_router(equipment.router,      prefix="/api/v1")
app.include_router(reports.router,        prefix="/api/v1")
app.include_router(dashboard.router,      prefix="/api/v1")
app.include_router(analytics.router,      prefix="/api/v1")
app.include_router(equipment_live.router, prefix="/api/v1")
app.include_router(reports_v2.router,     prefix="/api/v1")
app.include_router(scheduled.router,      prefix="/api/v1")
app.include_router(tracker.router,        prefix="/api/v1")
app.include_router(isolator.router,       prefix="/api/v1")


# ── Root ──────────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def root():
    return {
        "service":           "GE Solar Monitoring API",
        "version":           "1.0.0",
        "docs":              "/docs",
        "health":            "/health",
        "db_test":           "/db-test",
        "dashboard_kpis":    "/api/v1/dashboard/kpis",
        "power_trend":       "/api/v1/dashboard/power-trend",
        "daily_energy":      "/api/v1/dashboard/daily-energy",
        "alarms":            "/api/v1/dashboard/alarms",
        "equipment_live":    "/api/v1/equipment-live/",
        "analytics":         "/api/v1/analytics/inverter-comparison",
        "reports":           "/api/v1/reports/data",
        "equipment_types":   "/api/v1/reports-v2/equipment-types",
        "equipment_list":    "/api/v1/reports-v2/equipment-list?type=Inverter",
        "tags":              "/api/v1/reports-v2/tags?equipment_type=Inverter&equipment_id=INVERTER_01",
        "report_data":       "/api/v1/reports-v2/data",
        "wms":               "/api/v1/reports/wms/latest",
        "ppc":               "/api/v1/reports/ppc/latest",
    }