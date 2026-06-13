"""Quota checking service for Dograh credits.

This module provides reusable quota checking functionality that can be used
across different endpoints (WebRTC signaling, telephony, public API triggers).
"""

import os
import httpx
from dataclasses import dataclass
from loguru import logger

from api.db import db_client
from api.db.models import UserModel
from api.services.configuration.registry import ServiceProviders
from api.services.configuration.resolve import resolve_effective_config
from api.services.mps_service_key_client import mps_service_key_client

# ========= TAMALE quota integration (A.2) =========
TAMALE_BASE_URL = os.getenv("TAMALE_BASE_URL", "http://172.17.0.1:3000")
TAMALE_WEBHOOK_SECRET = os.getenv("DOGRAH_WEBHOOK_SECRET", "")
TAMALE_QUOTA_TIMEOUT = float(os.getenv("TAMALE_QUOTA_TIMEOUT", "2.0"))

async def _check_tamale_quota(dograh_org_id: int) -> dict:
    """Call TAMALE's voice quota endpoint. Fail-open on any error."""
    if not TAMALE_WEBHOOK_SECRET:
        logger.warning("[TamaleQuota] DOGRAH_WEBHOOK_SECRET not set — fail-open")
        return {"allowed": True, "reason": "no_secret_fail_open"}

    url = f"{TAMALE_BASE_URL}/api/v1/voice_quota/check"
    headers = {"Authorization": f"Bearer {TAMALE_WEBHOOK_SECRET}"}
    params = {"dograh_org_id": dograh_org_id}

    try:
        async with httpx.AsyncClient(timeout=TAMALE_QUOTA_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            logger.warning(
                f"[TamaleQuota] non-200 org={dograh_org_id} status={resp.status_code} "
                f"body={resp.text[:200]} — fail-open"
            )
            return {"allowed": True, "reason": f"http_{resp.status_code}_fail_open"}
        data = resp.json()
        logger.info(
            f"[TamaleQuota] org={dograh_org_id} allowed={data.get('allowed')} "
            f"used={data.get('voice_minutes_used')}/{data.get('voice_minutes_limit')}m"
        )
        return data
    except Exception as e:
        logger.error(f"[TamaleQuota] exception org={dograh_org_id}: {e} — fail-open")
        return {"allowed": True, "reason": "exception_fail_open"}


@dataclass
class QuotaCheckResult:
    """Result of a quota check."""
    has_quota: bool
    error_message: str = ""
    error_code: str = ""


async def check_dograh_quota(
    user: UserModel, workflow_id: int | None = None
) -> QuotaCheckResult:
    """Check if user has sufficient Dograh quota for making a call."""
    try:
        # Get user configurations
        user_config = await db_client.get_user_configurations(user.id)

        if workflow_id is not None:
            workflow = await db_client.get_workflow_by_id(workflow_id)
            if workflow:
                model_overrides = (workflow.workflow_configurations or {}).get(
                    "model_overrides"
                )
                if model_overrides:
                    user_config = resolve_effective_config(user_config, model_overrides)

        # Check if user is using any Dograh service
        using_dograh = False
        dograh_api_keys = set()

        if user_config.llm and user_config.llm.provider == ServiceProviders.DOGRAH:
            using_dograh = True
            dograh_api_keys.add(user_config.llm.api_key)

        if user_config.stt and user_config.stt.provider == ServiceProviders.DOGRAH:
            using_dograh = True
            dograh_api_keys.add(user_config.stt.api_key)

        if user_config.tts and user_config.tts.provider == ServiceProviders.DOGRAH:
            using_dograh = True
            dograh_api_keys.add(user_config.tts.api_key)

        # If using Dograh, check quota for ALL Dograh keys
        if using_dograh:
            for api_key in dograh_api_keys:
                try:
                    usage = await mps_service_key_client.check_service_key_usage(
                        api_key, created_by=user.provider_id
                    )
                    remaining = usage.get("remaining_credits", 0.0)

                    # Require at least $0.10 for a short call
                    if remaining < 0.10:
                        logger.warning(
                            f"Insufficient Dograh credits for key ...{api_key[-8:]}: "
                            f"${remaining:.2f} remaining"
                        )
                        return QuotaCheckResult(
                            has_quota=False,
                            error_code="quota_exceeded",
                            error_message=(
                                "You have exhausted your trial credits. "
                                "Please email founders@dograh.com for additional Dograh credits "
                                "or change providers in Models configurations."
                            ),
                        )

                    logger.info(
                        f"Dograh quota check passed for key ...{api_key[-8:]}: "
                        f"{remaining:.2f} credits remaining"
                    )
                except Exception as e:
                    logger.error(f"Failed to check quota for Dograh key: {str(e)}")
                    error_str = str(e)
                    if "404" in error_str or "not found" in error_str.lower():
                        return QuotaCheckResult(
                            has_quota=False,
                            error_code="invalid_service_key",
                            error_message="You have invalid keys in your model configuration. Please validate the service keys.",
                        )
                    return QuotaCheckResult(
                        has_quota=False,
                        error_code="quota_check_failed",
                        error_message="Could not verify Dograh credits. Please try again.",
                    )

        # ----- TAMALE billing quota check (A.2 addition) -----
        org_id = getattr(user, "selected_organization_id", None)
        if org_id:
            tamale_result = await _check_tamale_quota(org_id)
            if not tamale_result.get("allowed", True):
                used = tamale_result.get("voice_minutes_used")
                limit = tamale_result.get("voice_minutes_limit")
                return QuotaCheckResult(
                    has_quota=False,
                    error_code="quota_exceeded",
                    error_message=(
                        f"Voice minutes exhausted ({used}/{limit}m). "
                        "Please upgrade your plan or wait for renewal."
                    )
                )

        return QuotaCheckResult(has_quota=True)

    except Exception as e:
        logger.error(f"Error during quota check: {str(e)}")
        # On unexpected error, allow the call to proceed
        return QuotaCheckResult(has_quota=True)


async def check_dograh_quota_by_user_id(
    user_id: int, workflow_id: int | None = None
) -> QuotaCheckResult:
    """Check Dograh quota by user ID."""
    user = await db_client.get_user_by_id(user_id)
    if not user:
        return QuotaCheckResult(
            has_quota=False,
            error_message="User not found",
            error_code="user_not_found"
        )
    return await check_dograh_quota(user, workflow_id=workflow_id)
