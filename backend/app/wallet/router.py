import logging

from fastapi import APIRouter, HTTPException, status

from app.auth.dependencies import CurrentUserDependency, SettingsDependency
from app.wallet.dependencies import WalletServiceDependency
from app.wallet.schemas import (
    RechargeBody,
    RechargeResponse,
    WalletResponse,
    WalletTransactionResponse,
)
from app.wallet.service import RechargeAmountError

logger = logging.getLogger("talkstream.wallet")
router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("", response_model=WalletResponse)
async def get_wallet(
    user: CurrentUserDependency,
    settings: SettingsDependency,
    wallet: WalletServiceDependency,
) -> WalletResponse:
    balance = await wallet.balance(user.id)
    transactions = await wallet.recent_transactions(user.id)
    return WalletResponse(
        balance_paise=balance,
        price_per_minute_paise=settings.price_per_minute_paise,
        recent_transactions=[
            WalletTransactionResponse.model_validate(item) for item in transactions
        ],
    )


@router.post("/recharge", response_model=RechargeResponse)
async def recharge_wallet(
    body: RechargeBody,
    user: CurrentUserDependency,
    settings: SettingsDependency,
    wallet: WalletServiceDependency,
) -> RechargeResponse:
    try:
        transaction, balance = await wallet.recharge(
            user_id=user.id,
            amount_paise=body.amount_paise,
            max_amount_paise=settings.max_recharge_paise,
        )
    except RechargeAmountError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    logger.info(
        "mock_recharge user_id=%s amount_paise=%s balance_paise=%s",
        user.id,
        body.amount_paise,
        balance,
    )
    return RechargeResponse(
        balance_paise=balance,
        transaction=WalletTransactionResponse.model_validate(transaction),
    )
