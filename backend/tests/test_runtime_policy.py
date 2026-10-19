from __future__ import annotations

import importlib
import importlib.util
import secrets

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings


def policy_class():
    assert importlib.util.find_spec("app.runtime") is not None, "No bounded runtime policy"
    return importlib.import_module("app.runtime").RuntimePolicy


@pytest.mark.parametrize(
    ("profile", "month", "day", "hosting"),
    [
        ("owner_hosted", 1_000_000_000, 30_000_000, 500_000_000),
        ("limited_vps", 500_000_000, 15_000_000, 1_000_000_000),
    ],
)
def test_complete_profiles_fit_operating_ceiling(profile, month, day, hosting):
    policy = policy_class()(profile=profile)
    assert policy.variable_month_micro_inr == month
    assert policy.global_day_micro_inr == day
    assert policy.hosting_month_micro_inr == hosting
    assert month + hosting + policy.reserve_micro_inr == 2_000_000_000


def test_default_call_allowance_fits_a_coherent_call():
    policy = policy_class()()
    assert policy.session_micro_inr == 15_000_000
    assert policy.user_day_micro_inr == 30_000_000
    # A hosted user must still be able to start one call on a fresh day.
    assert policy.session_micro_inr <= policy.user_day_micro_inr <= policy.global_day_micro_inr


def test_profile_cannot_mix_independent_allowances():
    with pytest.raises(ValidationError):
        policy_class()(profile="limited_vps", variable_month_micro_inr=1_000_000_000)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), 0, -1])
def test_nonfinite_or_nonpositive_deadlines_are_rejected(deadline):
    with pytest.raises(ValidationError):
        policy_class()(initialization_seconds=deadline)


def test_policy_is_immutable():
    policy = policy_class()()
    with pytest.raises(ValidationError):
        policy.global_active_calls = 100


def test_runtime_rejects_multiple_workers():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, backend_workers=2)


def test_public_startup_rejects_development_configuration():
    with pytest.raises(ValidationError, match="public"):
        Settings(_env_file=None, app_env="production", jwt_secret="replace-this-secret" * 3)


def test_public_configuration_requires_local_policy_and_rates(tmp_path):
    with pytest.raises(ValidationError, match="public"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret=secrets.token_hex(32),
            resend_api_key="test-only-email-key",
            resend_from="Talkstream <login@example.com>",
            admin_emails="owner@example.com",
            credential_encryption_key=Fernet.generate_key().decode(),
            backup_encryption_key=secrets.token_urlsafe(32),
            public_origin="https://demo.example.com",
            cors_origins="https://demo.example.com",
            policy_file=tmp_path / "absent-policy.json",
            rate_card_file=tmp_path / "absent-rates.json",
        )


def test_no_live_evaluation_enabled_by_default():
    settings = Settings(_env_file=None)
    assert getattr(settings, "live_evaluation_cap_micro_inr", None) == 0


@pytest.fixture
def public_values(tmp_path):
    policy = tmp_path / "policy.json"
    rates = tmp_path / "rates.json"
    policy.write_text("{}")
    rates.write_text("{}")
    return {
        "app_env": "production",
        "jwt_secret": secrets.token_hex(32),
        "resend_api_key": "test-only-email-key",
        "resend_from": "Talkstream <login@example.com>",
        "admin_emails": "owner@example.com",
        "credential_encryption_key": Fernet.generate_key().decode(),
        "backup_encryption_key": secrets.token_urlsafe(32),
        "public_origin": "https://demo.example.com",
        "cors_origins": "https://demo.example.com",
        "policy_file": policy,
        "rate_card_file": rates,
        "recovery_journal_file": tmp_path / "deletions.jsonl",
    }


def test_complete_local_public_configuration_needs_no_network(public_values):
    assert Settings(_env_file=None, **public_values).app_env == "production"


@pytest.mark.parametrize("environment", ["PRODUCTION", "prod", "stagin"])
def test_unknown_environment_cannot_skip_public_validation(environment):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env=environment)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("credential_encryption_key", "short"),
        ("backup_encryption_key", ""),
        ("resend_api_key", ""),
        ("admin_emails", "not an email"),
        ("recovery_journal_file", "/missing-recovery-mount/deletions.jsonl"),
    ],
)
def test_public_configuration_rejects_invalid_credentials(public_values, key, value):
    public_values[key] = value
    with pytest.raises(ValidationError, match="public"):
        Settings(_env_file=None, **public_values)


def test_reserve_cannot_inflate_operating_ceiling():
    with pytest.raises(ValidationError):
        policy_class()(reserve_micro_inr=1_000_000_000)


@pytest.mark.parametrize("field", ["pdf_pages", "stored_chunks", "otp_ttl_seconds"])
def test_nonpositive_resource_limits_are_rejected(field):
    with pytest.raises(ValidationError):
        policy_class()(**{field: -1})


def test_public_keys_can_be_loaded_from_mounted_secret_files(public_values, tmp_path, monkeypatch):
    for field in ("credential_encryption_key", "backup_encryption_key"):
        value = public_values.pop(field)
        keyfile = tmp_path / field
        keyfile.write_text(value)
        monkeypatch.setenv(f"{field.upper()}_FILE", str(keyfile))
    settings = Settings(_env_file=None, **public_values)
    assert settings.credential_encryption_key is not None
    assert settings.backup_encryption_key is not None


def test_configuration_error_text_does_not_disclose_secret_input():
    secret = "public-secret-prefix-abcdefghijklmnopqrstuvwxyz-0123456789-secret-end"
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, app_env="production", jwt_secret=secret)
    assert secret[-12:] not in str(error.value)


def test_blank_optional_file_settings_stay_unconfigured(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POLICY_FILE=\nRATE_CARD_FILE=\nPUBLIC_ORIGIN=\n")
    settings = Settings(_env_file=env_file)
    assert settings.policy_file is None
    assert settings.rate_card_file is None
    assert settings.public_origin is None
