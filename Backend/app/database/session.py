# app/database/session.py

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool
from typing import Generator
import logging

from app.database.connection import build_sqlalchemy_url

logger = logging.getLogger(__name__)

# ── Engine ────────────────────────────────────────────────────────────────────
engine = create_engine(
    build_sqlalchemy_url(),
    poolclass=QueuePool,
    pool_size=10,            # number of connections to keep open
    max_overflow=20,         # extra connections beyond pool_size
    pool_timeout=30,         # seconds to wait for a connection
    pool_recycle=1800,       # recycle connections after 30 min
    pool_pre_ping=True,      # test connection health before using
    echo=False,              # set True to log all SQL (dev only)
    connect_args={
        "timeout": 30,
    },
)


# ── Connection event: verify connection on checkout ───────────────────────────
@event.listens_for(engine, "connect")
def on_connect(dbapi_connection, connection_record):
    """Fired when a new DBAPI connection is created."""
    logger.info("New database connection established.")


@event.listens_for(engine, "checkout")
def on_checkout(dbapi_connection, connection_record, connection_proxy):
    """Fired on every connection checkout from pool."""
    logger.debug("Database connection checked out from pool.")


# ── Session Factory ───────────────────────────────────────────────────────────
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


# ── Dependency Injection ──────────────────────────────────────────────────────
def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that provides a database session per request.
    Automatically commits on success and rolls back on exception.
    Always closes the session when the request is done.

    Usage:
        @router.get("/items")
        def get_items(db: Session = Depends(get_db)):
            ...
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def verify_database_connection() -> bool:
    """
    Verify the SQLAlchemy engine can connect to the database.
    Used at application startup.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection verified successfully.")
        return True
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return False