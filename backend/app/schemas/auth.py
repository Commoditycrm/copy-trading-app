import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.user import UserRole


def _normalize_email(v: object) -> object:
    """Strip whitespace and lowercase the email so user identity is
    case-insensitive end-to-end. Runs in pydantic's ``before`` phase so
    EmailStr's format validation sees the already-normalized form, and
    the User.email lookup in app/api/auth.py (case-sensitive equality)
    matches what we stored at registration regardless of how the user
    typed their email at sign-in time."""
    if isinstance(v, str):
        return v.strip().lower()
    return v


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: UserRole
    display_name: str | None = Field(default=None, max_length=120)
    # Mandatory for traders — surfaced as the app name in the shell for
    # the trader and every subscriber who follows them. Ignored for
    # subscribers (they inherit the brand from the trader they follow).
    business_name: str | None = Field(default=None, max_length=120)

    _norm_email = field_validator("email", mode="before")(_normalize_email)

    @model_validator(mode="after")
    def _require_business_name_for_trader(self) -> "RegisterIn":
        if self.role == UserRole.TRADER:
            name = (self.business_name or "").strip()
            if not name:
                raise ValueError("business_name is required for traders")
            self.business_name = name
        else:
            # Subscribers + admins never carry a brand of their own.
            self.business_name = None
        return self


class LoginIn(BaseModel):
    email: EmailStr
    password: str

    _norm_email = field_validator("email", mode="before")(_normalize_email)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    role: UserRole
    display_name: str | None
    business_name: str | None = None
    is_active: bool

    model_config = {"from_attributes": True}
