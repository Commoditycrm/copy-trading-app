import uuid

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.user import UserRole


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: UserRole
    display_name: str | None = Field(default=None, max_length=120)
    # Mandatory for traders — surfaced as the app name in the shell for
    # the trader and every subscriber who follows them. Ignored for
    # subscribers (they inherit the brand from the trader they follow).
    business_name: str | None = Field(default=None, max_length=120)

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
