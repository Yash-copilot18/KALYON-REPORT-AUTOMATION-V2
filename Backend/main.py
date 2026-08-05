<<<<<<< HEAD
from app.main import app

__all__ = ["app"]
=======
# Backend/main.py
#
# The real application lives in app/main.py — it registers every router under
# /api/v1, configures CORS, and opens the SQL Server connection pool on startup.
# This module only re-exports it so that both of these serve the same app:
#
#     uvicorn main:app
#     uvicorn app.main:app
#
# This file previously defined its own bare FastAPI() instance with a single "/"
# route. Because it shadowed the real app, `uvicorn main:app` started a server
# that answered / but 404'd every /api/v1/* request.

from app.main import app

__all__ = ["app"]
>>>>>>> 24d225b9418987e03a56ca021d90ff50abec28de
