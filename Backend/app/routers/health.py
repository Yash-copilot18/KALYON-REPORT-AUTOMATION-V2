# app/routers/health.py

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text
from app.database.session import engine
from app.database.connection import test_raw_connection
import time

router = APIRouter(tags=["Health"])


@router.get("/health", summary="Application health check")
def health_check():
    return {
        "status": "ok",
        "service": "GE Solar Monitoring API",
        "version": "1.0.0",
    }


@router.get("/db-test", summary="Test SQL Server database connectivity")
def db_test():
    """
    Verifies live connectivity to SQL Server using Windows Authentication.
    Returns server name, database name, and SQL Server version.
    """
    start = time.perf_counter()
    try:
        result = test_raw_connection()
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "status": "connected",
            "latency_ms": elapsed_ms,
            **result,
        }
    except ConnectionError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )


@router.get("/db-sqlalchemy-test", summary="Test SQLAlchemy engine connectivity")
def db_sqlalchemy_test():
    """
    Verifies the SQLAlchemy engine can execute a query against SQL Server.
    """
    start = time.perf_counter()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT @@VERSION AS version, @@SERVERNAME AS server, DB_NAME() AS db")
            ).fetchone()
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "status": "connected",
            "latency_ms": elapsed_ms,
            "server": row.server,
            "database": row.db,
            "version": row.version.split("\n")[0].strip(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"SQLAlchemy connection failed: {str(e)}",
        )