import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from app.modules.users.models import UserRole


def _normalize_email(value: str) -> str:
    # Регистр локальной части на практике не различают; приводим, чтобы не плодить дубли.
    return value.strip().lower()


NormalizedEmail = Annotated[EmailStr, AfterValidator(_normalize_email)]


class RegisterRequest(BaseModel):
    email: NormalizedEmail
    # Верхний предел защищает от огромных «паролей», на хэширование которых уходил бы CPU.
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: NormalizedEmail
    # Минимальную длину при входе не проверяем: ответ не должен раскрывать правила пароля.
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105  (тип схемы авторизации, не секрет)
    expires_in: int  # срок жизни access-токена, секунды


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: UserRole
    created_at: datetime
