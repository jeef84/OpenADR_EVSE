"""Thin OpenADR 3.1 BL/VEN helpers around openadr3-client."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from openadr3_client.bl.http_factory import BusinessLogicHttpClientFactory
from openadr3_client.oadr310.models.event.event import EventPayload, Interval, NewEvent
from openadr3_client.oadr310.models.event.event_payload import EventPayloadDescriptor, EventPayloadType
from openadr3_client.oadr310.models.program.program import IntervalPeriod, NewProgram
from openadr3_client.oadr310.models.unit import Unit
from openadr3_client.ven.http_factory import VirtualEndNodeHttpClientFactory
from openadr3_client.version import OADRVersion

logger = logging.getLogger(__name__)

PROGRAM_NAME = "HOME_EV_FLEX"

# Plan concept IMPORT_POWER_LIMIT maps to OpenADR 3.1 IMPORT_CAPACITY_LIMIT (kW).
PRICE_TYPE = EventPayloadType.PRICE
IMPORT_LIMIT_TYPE = EventPayloadType.IMPORT_CAPACITY_LIMIT

BL_SCOPES = [
    "read_all",
    "write_vens_bl",
    "write_programs",
    "write_events",
    "write_users",
    "write_subscriptions_bl",
]
VEN_SCOPES = [
    "read_targets",
    "read_ven_objects",
    "write_reports",
    "write_subscriptions_ven",
    "write_vens_ven",
]


def _allow_lab_http_oauth(token_url: str) -> None:
    """
    oauthlib blocks http:// token URLs unless this env is set.

    openadr3-client's allow_insecure_http covers VTN REST, not the token fetch.
    Lab-only; never enable for production HTTPS deployments.
    """
    if token_url.startswith("https://"):
        return
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def create_bl_client(
    *,
    vtn_base_url: str,
    client_id: str,
    client_secret: str,
    token_url: str,
) -> Any:
    _allow_lab_http_oauth(token_url)
    return BusinessLogicHttpClientFactory.create_http_bl_client(
        vtn_base_url=vtn_base_url,
        client_id=client_id,
        client_secret=client_secret,
        token_url=token_url,
        scopes=BL_SCOPES,
        version=OADRVersion.OADR_310,
        verify_vtn_tls_certificate=False,
        allow_insecure_http=True,
    )


def create_ven_client(
    *,
    vtn_base_url: str,
    client_id: str,
    client_secret: str,
    token_url: str,
) -> Any:
    _allow_lab_http_oauth(token_url)
    return VirtualEndNodeHttpClientFactory.create_http_ven_client(
        vtn_base_url=vtn_base_url,
        client_id=client_id,
        client_secret=client_secret,
        token_url=token_url,
        scopes=VEN_SCOPES,
        version=OADRVersion.OADR_310,
        verify_vtn_tls_certificate=False,
        allow_insecure_http=True,
    )


def _payload_descriptors() -> tuple[EventPayloadDescriptor, EventPayloadDescriptor]:
    return (
        EventPayloadDescriptor(
            payload_type=PRICE_TYPE,
            units=Unit.KWH,
            currency="USD",
        ),
        EventPayloadDescriptor(
            payload_type=IMPORT_LIMIT_TYPE,
            units=Unit.KW,
        ),
    )


def ensure_program(bl_client: Any) -> str:
    """Return HOME_EV_FLEX program id, creating the program if missing."""
    programs = bl_client.programs.get_programs(target=None, pagination=None)
    for program in programs:
        if program.program_name == PROGRAM_NAME:
            return program.id

    program = NewProgram(
        program_name=PROGRAM_NAME,
        payload_descriptors=_payload_descriptors(),
    )
    created = bl_client.programs.create_program(new_program=program)
    logger.info("Created OpenADR program %s id=%s", PROGRAM_NAME, created.id)
    return created.id


def upsert_flex_event(
    bl_client: Any,
    *,
    program_id: str,
    marginal_price: float | None,
    import_power_limit_kw: float,
    duration: timedelta = timedelta(minutes=15),
    existing_event_id: str | None = None,
) -> str:
    """
    Create or replace the active PRICE + IMPORT_CAPACITY_LIMIT event.

    When marginal_price is None (no accepted blocks), publish PRICE as a high
    sentinel so the VEN stays off unless Charge Now is active.
    """
    price = 999.0 if marginal_price is None else marginal_price
    start = datetime.now(tz=UTC)
    event = NewEvent(
        program_id=program_id,
        event_name="home-ev-flex-active",
        priority=1,
        payload_descriptors=_payload_descriptors(),
        interval_period=IntervalPeriod(start=start, duration=duration),
        intervals=(
            Interval(
                id=0,
                interval_period=None,
                payloads=(
                    EventPayload(type=PRICE_TYPE, values=(price,)),
                    EventPayload(type=IMPORT_LIMIT_TYPE, values=(import_power_limit_kw,)),
                ),
            ),
        ),
    )

    if existing_event_id:
        try:
            bl_client.events.delete_event_by_id(event_id=existing_event_id)
        except Exception:  # noqa: BLE001 — best-effort replace
            logger.warning("Could not delete prior event %s", existing_event_id, exc_info=True)

    created = bl_client.events.create_event(new_event=event)
    return created.id


def read_active_flex_signals(ven_client: Any, *, program_id: str | None = None) -> dict[str, float | None]:
    """Poll VTN events and extract PRICE ($/kWh) and import capacity limit (kW)."""
    events = ven_client.events.get_events(target=None, pagination=None, program_id=program_id)
    price: float | None = None
    import_limit: float | None = None

    for event in events:
        for interval in event.intervals or ():
            for payload in interval.payloads or ():
                values = payload.values or ()
                if not values:
                    continue
                value = float(values[0])
                type_str = str(payload.type)
                if type_str == "PRICE":
                    price = value
                elif type_str in {"IMPORT_CAPACITY_LIMIT", "IMPORT_POWER_LIMIT"}:
                    import_limit = value

    return {"price": price, "import_power_limit_kw": import_limit}
