import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.user import UserRole

# Roles a user may assign to themselves at sign-up. ADMIN is deliberately
# excluded — admins are provisioned out-of-band (seed/migration or promoted
# by an existing admin via PATCH /api/admin/users/{id}/role). Without this
# gate, register() trusted the request body's `role` verbatim, so anyone
# could POST {"role": "admin"} and self-provision a platform administrator.
_SELF_REGISTRABLE_ROLES = frozenset({UserRole.TRADER, UserRole.SUBSCRIBER})

# bcrypt only hashes the first 72 BYTES of a password and silently drops the
# rest. We cap at the byte boundary so a long passphrase can't be truncated
# behind the user's back (two passwords sharing a 72-byte prefix would
# otherwise verify against the same hash).
_BCRYPT_MAX_BYTES = 72


def _validate_password_strength(pw: str) -> str:
    """Enforce a minimal password policy: 8+ chars, within bcrypt's
    72-byte limit, and at least three of {lowercase, uppercase, digit,
    symbol}. Raises ValueError (surfaced by pydantic as a 422)."""
    if len(pw) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(pw.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise ValueError("password must be at most 72 bytes long")
    classes = (
        any(c.islower() for c in pw),
        any(c.isupper() for c in pw),
        any(c.isdigit() for c in pw),
        any(not c.isalnum() for c in pw),
    )
    if sum(classes) < 3:
        raise ValueError(
            "password must include at least three of: lowercase letter, "
            "uppercase letter, digit, symbol"
        )
    return pw


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
    password: str = Field(min_length=8, max_length=72)
    role: UserRole
    display_name: str | None = Field(default=None, max_length=120)
    # Mandatory for traders — surfaced as the app name in the shell for
    # the trader and every subscriber who follows them. Ignored for
    # subscribers (they inherit the brand from the trader they follow).
    business_name: str | None = Field(default=None, max_length=120)

    _norm_email = field_validator("email", mode="before")(_normalize_email)

    @field_validator("password")
    @classmethod
    def _password_policy(cls, v: str) -> str:
        return _validate_password_strength(v)

    @model_validator(mode="after")
    def _require_business_name_for_trader(self) -> "RegisterIn":
        # Block privilege escalation: only self-registrable roles allowed.
        if self.role not in _SELF_REGISTRABLE_ROLES:
            raise ValueError("role must be 'trader' or 'subscriber'")
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


class RefreshIn(BaseModel):
    # Carried in the request BODY, not the query string. A refresh token in
    # the URL leaks into proxy/access logs and browser history.
    refresh_token: str = Field(min_length=1)


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    # Same constraints as registration so a reset can't set a weaker password.
    new_password: str = Field(min_length=8, max_length=128)


class VerifyEmailIn(BaseModel):
    token: str


class ResendVerificationIn(BaseModel):
    email: EmailStr


class MessageOut(BaseModel):
    detail: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    # Same constraints as registration so a reset can't set a weaker password.
    new_password: str = Field(min_length=8, max_length=128)


class MessageOut(BaseModel):
    detail: str


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
    email_verified: bool = True

    model_config = {"from_attributes": True}
