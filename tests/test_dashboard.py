from unittest.mock import patch

from dashboard.ui import _build_dashboard_config, _normalize_session_state, render_custom_dashboard


def test_build_dashboard_config_uses_strategy_tokens_and_pool_type():
    config = _build_dashboard_config(
        {
            "base_token": "WETH",
            "quote_token": "USDC",
            "chain": "base",
            "pool": "WETH/USDC/volatile",
        }
    )

    assert config.protocol == "aerodrome"
    assert config.token0 == "WETH"
    assert config.token1 == "USDC"
    assert config.fee_tier == "volatile"
    assert config.chain == "base"


def test_normalize_session_state_maps_last_position_value():
    normalized = _normalize_session_state({"last_position_usd": "123.45"})

    assert normalized["total_value_usd"] == "123.45"


def test_render_custom_dashboard_prepares_and_renders():
    with patch("dashboard.ui.prepare_lp_session_state", return_value={"position_id": None}) as prepare_mock:
        with patch("dashboard.ui.render_lp_dashboard") as render_mock:
            render_custom_dashboard(
                strategy_id="conservative_base_lp",
                strategy_config={
                    "base_token": "WETH",
                    "quote_token": "USDC",
                    "chain": "base",
                    "pool": "WETH/USDC/volatile",
                    "protocol": "aerodrome",
                },
                api_client=object(),
                session_state={"last_position_usd": "50"},
            )

    prepare_mock.assert_called_once()
    _, kwargs = prepare_mock.call_args
    config = kwargs["config"]
    assert config.protocol == "aerodrome"
    assert kwargs["session_state"]["total_value_usd"] == "50"

    render_mock.assert_called_once()
    args, _ = render_mock.call_args
    assert args[0] == "conservative_base_lp"
    assert args[2] == {"position_id": None}
    assert args[3].protocol == "aerodrome"
