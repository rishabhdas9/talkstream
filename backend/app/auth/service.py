from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.email import OtpDeliveryError, OtpEmailSender
from app.auth.limits import reserve_otp_send
from app.auth.otp import OtpSnapshot, OtpStatus, assess_otp, generate_otp, hash_otp
from app.auth.tokens import encode_access_token
from app.config import Settings
from app.models import OtpChallenge, User, new_id
from app.runtime import Clock, RuntimePolicy, SystemClock

DEV_LOGIN_EMAIL = "user@example.com"


class InvalidOtpError(ValueError):
    pass


class AuthService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        email_sender: OtpEmailSender,
        clock: Clock | None = None,
        policy: RuntimePolicy | None = None,
        client_ip: str = "unknown",
    ) -> None:
        self._session = session
        self._settings = settings
        self._email_sender = email_sender
        self._clock = clock or SystemClock()
        self._policy = policy or RuntimePolicy(profile=settings.runtime_profile)
        self._client_ip = client_ip

    @property
    def otp_ttl_seconds(self) -> int:
        return min(self._settings.otp_ttl_seconds, self._policy.otp_ttl_seconds)

    @property
    def access_token_ttl_seconds(self) -> int:
        return min(self._settings.jwt_access_ttl_seconds, self._policy.access_ttl_seconds)

    async def request_otp(self, email: str) -> None:
        normalized_email = email.strip().lower()
        challenge_id = new_id()
        code = generate_otp()
        now = self._clock.utc_now()
        async with self._session.begin():
            await self._session.execute(text("BEGIN IMMEDIATE"))
            await reserve_otp_send(
                self._session,
                email=normalized_email,
                ip=self._client_ip,
                now=now,
                policy=self._policy,
                pepper=self._settings.jwt_secret_value,
            )
            await self._session.execute(
                update(OtpChallenge)
                .where(OtpChallenge.email == normalized_email, OtpChallenge.consumed.is_(False))
                .values(consumed=True)
                .execution_options(synchronize_session=False)
            )
            challenge = OtpChallenge(
                id=challenge_id,
                email=normalized_email,
                code_hash=hash_otp(
                    challenge_id=challenge_id,
                    email=normalized_email,
                    code=code,
                    pepper=self._settings.jwt_secret_value,
                ),
                expires_at=now + timedelta(seconds=self.otp_ttl_seconds),
                created_at=now,
            )
            self._session.add(challenge)

        try:
            await self._email_sender.send_otp(
                email=normalized_email,
                code=code,
                challenge_id=challenge_id,
            )
        except OtpDeliveryError:
            challenge.consumed = True
            await self._session.commit()
            raise

    async def verify_otp(self, email: str, code: str) -> str:
        normalized_email = email.strip().lower()
        challenge = await self._latest_challenge(normalized_email)
        if challenge is None:
            raise InvalidOtpError("Invalid or expired OTP")

        decision = assess_otp(
            OtpSnapshot(
                id=challenge.id,
                email=challenge.email,
                code_hash=challenge.code_hash,
                expires_at=challenge.expires_at,
                attempts=challenge.attempts,
                consumed=challenge.consumed,
            ),
            presented_code=code,
            pepper=self._settings.jwt_secret_value,
            max_attempts=min(self._settings.otp_max_attempts, self._policy.otp_max_attempts),
            now=self._clock.utc_now(),
        )

        if decision.status is OtpStatus.ACCEPTED:
            consumed = await self._session.execute(
                update(OtpChallenge)
                .where(
                    OtpChallenge.id == challenge.id,
                    OtpChallenge.consumed.is_(False),
                    OtpChallenge.attempts
                    < min(self._settings.otp_max_attempts, self._policy.otp_max_attempts),
                    OtpChallenge.expires_at > self._clock.utc_now(),
                )
                .values(consumed=True)
                .execution_options(synchronize_session=False)
            )
            if consumed.rowcount != 1:
                await self._session.rollback()
                raise InvalidOtpError("Invalid or expired OTP")
            user = await self._find_or_create_user(normalized_email)
            await self._session.commit()
            return self._access_token(user)

        if decision.status is OtpStatus.INVALID or decision.status is OtpStatus.ATTEMPTS_EXHAUSTED:
            await self._session.execute(
                update(OtpChallenge)
                .where(OtpChallenge.id == challenge.id, OtpChallenge.consumed.is_(False))
                .values(
                    attempts=OtpChallenge.attempts + 1,
                    consumed=case(
                        (
                            OtpChallenge.attempts + 1
                            >= min(self._settings.otp_max_attempts, self._policy.otp_max_attempts),
                            True,
                        ),
                        else_=OtpChallenge.consumed,
                    ),
                )
                .execution_options(synchronize_session=False)
            )
        elif decision.consume:
            await self._session.execute(
                update(OtpChallenge)
                .where(OtpChallenge.id == challenge.id)
                .values(consumed=True)
                .execution_options(synchronize_session=False)
            )
        await self._session.commit()
        raise InvalidOtpError("Invalid or expired OTP")

    async def dev_login(self) -> str:
        """Sign in as the single local user without email; the router allows it only locally."""
        user = await self._find_or_create_user(DEV_LOGIN_EMAIL)
        await self._session.commit()
        return self._access_token(user)

    def _access_token(self, user: User) -> str:
        return encode_access_token(
            user_id=user.id,
            email=user.email,
            secret=self._settings.jwt_secret_value,
            algorithm=self._settings.jwt_algorithm,
            ttl_seconds=self.access_token_ttl_seconds,
            issuer=self._settings.jwt_issuer,
            audience=self._settings.jwt_audience,
            now=self._clock.utc_now(),
        )

    async def _latest_challenge(self, email: str) -> OtpChallenge | None:
        result = await self._session.execute(
            select(OtpChallenge)
            .where(OtpChallenge.email == email)
            .order_by(OtpChallenge.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_or_create_user(self, email: str) -> User:
        result = await self._session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(email=email, is_verified=True)
            self._session.add(user)
            await self._session.flush()
        elif not user.is_verified:
            user.is_verified = True
        return user
