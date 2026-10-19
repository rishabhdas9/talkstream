from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import TransactionKind


class WalletTransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    amount_paise: int
    kind: TransactionKind
    usage_session_id: str | None
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _naive_datetimes_are_utc(cls, value: datetime) -> datetime:
        # SQLite returns naive datetimes; stamp UTC so browsers localize.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class WalletResponse(BaseModel):
    balance_paise: int
    price_per_minute_paise: int
    recent_transactions: list[WalletTransactionResponse]


class RechargeBody(BaseModel):
    amount_paise: int = Field(gt=0)


class RechargeResponse(BaseModel):
    balance_paise: int
    transaction: WalletTransactionResponse
