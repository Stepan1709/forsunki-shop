"""Импорт всех моделей в одном месте, чтобы они попали в Base.metadata (нужно Alembic).

При добавлении модуля с моделями добавьте его сюда.
"""

from app.core import idempotency as idempotency_models  # noqa: F401
from app.core import inbox as inbox_models  # noqa: F401
from app.core import outbox as outbox_models  # noqa: F401
from app.modules.cart import models as cart_models  # noqa: F401
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.orders import models as orders_models  # noqa: F401
from app.modules.payments import models as payments_models  # noqa: F401
from app.modules.repair import models as repair_models  # noqa: F401
from app.modules.users import models as users_models  # noqa: F401
