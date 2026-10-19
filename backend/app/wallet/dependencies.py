from typing import Annotated

from fastapi import Depends, Request

from app.wallet.service import WalletService


def get_wallet_service(request: Request) -> WalletService:
    return request.app.state.wallet_service


WalletServiceDependency = Annotated[WalletService, Depends(get_wallet_service)]
