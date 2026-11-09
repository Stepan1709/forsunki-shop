"""Импорт всех моделей в одном месте, чтобы они попали в Base.metadata (нужно Alembic).

При добавлении модуля с моделями добавьте его сюда.
"""

from app.modules.users import models as users_models  # noqa: F401
