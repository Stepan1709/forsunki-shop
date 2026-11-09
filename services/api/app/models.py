"""Импорт всех моделей в одном месте, чтобы они попали в Base.metadata (нужно Alembic).

При добавлении модуля с моделями добавьте его сюда.
"""

from app.core import inbox as inbox_models  # noqa: F401
from app.core import outbox as outbox_models  # noqa: F401
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.users import models as users_models  # noqa: F401
