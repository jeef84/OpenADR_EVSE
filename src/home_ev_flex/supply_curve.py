"""Marginal-cost supply curve for EV charging ($/kWh economics, kW power blocks)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SupplyBlock:
    """One incremental power block offered to the EV."""

    source: str
    available_kw: float
    price_per_kwh: float


@dataclass(frozen=True)
class DispatchResult:
    """Accepted continuous power and OpenADR signal values."""

    accepted_power_kw: float
    effective_marginal_price: float | None
    import_power_limit_kw: float
    supply_curve: tuple[SupplyBlock, ...]
    accepted_blocks: tuple[SupplyBlock, ...]


def build_supply_curve(
    *,
    solar_surplus_kw: float,
    export_credit_per_kwh: float,
    import_price_per_kwh: float,
    panel_service_headroom_kw: float,
) -> tuple[SupplyBlock, ...]:
    """
    Build the ordered marginal stack.

    Solar that would otherwise export is first (export opportunity cost).
    Remaining headroom is grid import at the current TOU retail rate.
    """
    blocks: list[SupplyBlock] = []
    surplus = max(0.0, solar_surplus_kw)
    headroom = max(0.0, panel_service_headroom_kw)

    solar_block = min(surplus, headroom)
    if solar_block > 0:
        blocks.append(
            SupplyBlock(
                source="solar_export_opportunity",
                available_kw=solar_block,
                price_per_kwh=export_credit_per_kwh,
            )
        )

    remaining_headroom = max(0.0, headroom - solar_block)
    if remaining_headroom > 0:
        blocks.append(
            SupplyBlock(
                source="grid_import",
                available_kw=remaining_headroom,
                price_per_kwh=import_price_per_kwh,
            )
        )

    return tuple(blocks)


def dispatch(
    supply_curve: tuple[SupplyBlock, ...],
    *,
    bid_price_per_kwh: float,
    evse_maximum_kw: float,
    vehicle_maximum_kw: float,
    panel_service_headroom_kw: float,
    user_charging_limit_kw: float,
) -> DispatchResult:
    """
    Accept blocks at or below the EV bid, then clamp by site/vehicle limits.

    Effective marginal price is the price of the highest accepted block.
    IMPORT power limit is the grid-import portion after solar-first acceptance
    (0 when only solar blocks clear the bid).
    """
    accepted = tuple(
        block
        for block in supply_curve
        if block.price_per_kwh <= bid_price_per_kwh and block.available_kw > 0
    )
    accepted_power = sum(b.available_kw for b in accepted)

    p_target = min(
        accepted_power,
        evse_maximum_kw,
        vehicle_maximum_kw,
        panel_service_headroom_kw,
        user_charging_limit_kw,
    )
    p_target = max(0.0, p_target)

    # Walk accepted blocks in order to attribute solar vs import after clamps.
    remaining = p_target
    import_kw = 0.0
    last_price: float | None = None
    for block in accepted:
        take = min(block.available_kw, remaining)
        if take <= 0:
            break
        if block.source == "grid_import":
            import_kw += take
        last_price = block.price_per_kwh
        remaining -= take

    return DispatchResult(
        accepted_power_kw=p_target,
        effective_marginal_price=last_price if p_target > 0 else None,
        import_power_limit_kw=import_kw,
        supply_curve=supply_curve,
        accepted_blocks=accepted,
    )
