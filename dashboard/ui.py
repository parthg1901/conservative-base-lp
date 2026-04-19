from typing import Any

from almanak.framework.dashboard.templates import (
    get_aerodrome_config,
    prepare_lp_session_state,
    render_lp_dashboard,
)


def _build_dashboard_config(strategy_config: dict[str, Any]):
    base_token = strategy_config.get("base_token", "WETH")
    quote_token = strategy_config.get("quote_token", "USDC")
    chain = strategy_config.get("chain", "base")

    pool = strategy_config.get("pool", "WETH/USDC/volatile")
    pool_parts = str(pool).split("/")
    pool_type = pool_parts[2] if len(pool_parts) >= 3 else "volatile"

    return get_aerodrome_config(
        token0=str(base_token),
        token1=str(quote_token),
        pool_type=str(pool_type),
        chain=str(chain),
    )


def _normalize_session_state(session_state: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(session_state or {})
    if "total_value_usd" not in normalized and "last_position_usd" in normalized:
        normalized["total_value_usd"] = normalized.get("last_position_usd", "0")
    return normalized


def render_custom_dashboard(
    strategy_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    config = _build_dashboard_config(strategy_config)
    prepared_state = prepare_lp_session_state(
        api_client,
        session_state=_normalize_session_state(session_state),
        config=config,
    )
    render_lp_dashboard(strategy_id, strategy_config, prepared_state, config)
