from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RequestOtpBody(BaseModel):
    email: EmailStr


class RequestOtpResponse(BaseModel):
    message: str
    expires_in_seconds: int


class VerifyOtpBody(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int


class CurrentUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    is_verified: bool


class VoiceTicketResponse(BaseModel):
    ticket: str
    expires_in_seconds: int
