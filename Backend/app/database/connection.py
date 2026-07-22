# app/database/connection.py

import pyodbc
import urllib
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class DatabaseSettings(BaseSettings):
    DB_SERVER: str = Field(..., env="DB_SERVER")
    DB_DATABASE: str = Field(..., env="DB_DATABASE")
    DB_DRIVER: str = Field(
        default="ODBC Driver 17 for SQL Server",
        env="DB_DRIVER"
    )

    model_config = {"env_file": ".env", "extra": "allow"}


@lru_cache()
def get_db_settings() -> DatabaseSettings:
    return DatabaseSettings()


def build_connection_string() -> str:
    """
    Build a pyodbc connection string using Windows Authentication
    (Trusted_Connection=yes) — no username or password required.
    """
    settings = get_db_settings()

    connection_string = (
        f"DRIVER={{{settings.DB_DRIVER}}};"
        f"SERVER={settings.DB_SERVER};"
        f"DATABASE={settings.DB_DATABASE};"
        f"Trusted_Connection=yes;"
        f"TrustServerCertificate=yes;"
    )
    return connection_string


def build_sqlalchemy_url() -> str:
    """
    Build the SQLAlchemy connection URL by URL-encoding the
    pyodbc connection string for use with create_engine().
    """
    connection_string = build_connection_string()
    encoded = urllib.parse.quote_plus(connection_string)
    return f"mssql+pyodbc:///?odbc_connect={encoded}"


def test_raw_connection() -> dict:
    """
    Test a raw pyodbc connection to SQL Server.
    Returns a dict with status and server info.
    Raises an exception if the connection fails.
    """
    conn_str = build_connection_string()
    try:
        conn = pyodbc.connect(conn_str, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT @@VERSION AS version, @@SERVERNAME AS server_name, DB_NAME() AS db_name")
        row = cursor.fetchone()
        result = {
            "status": "connected",
            "server_name": row.server_name,
            "database": row.db_name,
            "sql_server_version": row.version.split("\n")[0].strip(),
        }
        cursor.close()
        conn.close()
        return result
    except pyodbc.Error as e:
        raise ConnectionError(f"SQL Server connection failed: {str(e)}") from e