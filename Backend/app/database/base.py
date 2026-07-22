# app/database/base.py

from sqlalchemy.orm import DeclarativeBase, declared_attr
from sqlalchemy import Column, DateTime
from datetime import datetime, timezone

def import_all_models():
    from app.models import equipment  # noqa
    from app.models import reports    # noqa  ← add this line

class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy ORM models.
    Provides automatic table naming and shared audit columns.
    """

    @declared_attr.directive
    def __tablename__(cls) -> str:
        """
        Auto-generate table name from class name.
        e.g. Equipment -> equipment
             EquipmentType -> equipment_type
        """
        import re
        name = cls.__name__
        # Convert CamelCase to snake_case
        s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


# Import all models here so Alembic can discover them for migrations.
# Add new model imports below as you create them.
def import_all_models():
    from app.models import equipment  # noqa: F401