from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from almanak.framework.intents import Intent
from almanak.framework.teardown import TeardownMode
from strategy import ConservativeBaseLpStrategy


@pytest.fixture
def config() -> dict:
    return {
        "chain": "base",
        "protocol": "aerodrome",
        "pool": "WETH/USDC",
        "base_token": "WETH",
        "quote_token": "USDC",
        "range_width_pct": 20,
        "rebalance_drift_trigger_pct": "0.08",
        "min_rebalance_interval_minutes": 720,
        "reopen_cooldown_minutes": 180,
        "min_price_move_to_act_pct": "0.015",
        "min_rebalance_notional_usd": 40,
        "min_open_notional_usd": 50,
        "min_leg_usd": 20,
        "min_total_capital_usd": 80,
        "cash_reserve_usd": 15,
        "deploy_fraction": "0.85",
        "atr_period": 14,
        "atr_timeframe": "1h",
        "atr_emergency_pct": "0.035",
        "bb_period": 20,
        "bb_std_dev": 2.0,
        "bb_timeframe": "1h",
        "bb_bandwidth_emergency": "0.09",
        "high_vol_confirm_ticks": 2,
        "teardown_soft_slippage": "0.005",
        "teardown_hard_slippage": "0.02",
    }


@pytest.fixture
def strategy(config: dict) -> ConservativeBaseLpStrategy:
    return ConservativeBaseLpStrategy(
        config=config,
        chain="base",
        wallet_address="0x" + "1" * 40,
    )


def make_market(
    price: Decimal = Decimal("2000"),
    atr: Decimal = Decimal("20"),
    bandwidth: Decimal = Decimal("0.03"),
    weth_balance: Decimal = Decimal("0.04"),
    weth_usd: Decimal = Decimal("80"),
    usdc_balance: Decimal = Decimal("80"),
    usdc_usd: Decimal = Decimal("80"),
    timestamp: datetime | None = None,
):
    ts = timestamp or datetime.now(UTC)
    market = SimpleNamespace()
    market.timestamp = ts
    market.price = lambda token: price
    market.atr = lambda token, period=14, timeframe="1h": SimpleNamespace(value=atr)
    market.bollinger_bands = (
        lambda token, period=20, std_dev=2.0, timeframe="1h": SimpleNamespace(bandwidth=bandwidth)
    )

    def balance(token):
        if token == "WETH":
            return SimpleNamespace(balance=weth_balance, balance_usd=weth_usd)
        if token == "USDC":
            return SimpleNamespace(balance=usdc_balance, balance_usd=usdc_usd)
        raise ValueError("Unsupported token")

    market.balance = balance
    return market


def test_opens_lp_when_conditions_are_healthy(strategy: ConservativeBaseLpStrategy):
    market = make_market()
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_OPEN"
    assert intent.protocol == "aerodrome"
    assert intent.pool == "WETH/USDC"


def test_holds_on_insufficient_total_capital(strategy: ConservativeBaseLpStrategy):
    market = make_market(weth_usd=Decimal("20"), usdc_usd=Decimal("20"))
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "INSUFFICIENT_CAPITAL"


def test_holds_when_deployable_is_below_open_threshold(strategy: ConservativeBaseLpStrategy):
    strategy.min_open_notional_usd = Decimal("70")
    market = make_market(weth_usd=Decimal("45"), usdc_usd=Decimal("45"))
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "MIN_OPEN_NOTIONAL"


def test_enters_emergency_hold_after_high_vol_confirmation(strategy: ConservativeBaseLpStrategy):
    market = make_market(atr=Decimal("90"), bandwidth=Decimal("0.12"))

    first = strategy.decide(market)
    second = strategy.decide(market)

    assert first.intent_type.value == "LP_OPEN"
    assert second.intent_type.value == "HOLD"
    assert second.reason_code == "EMERGENCY_VOL_HOLD"


def test_holds_when_position_still_in_range(strategy: ConservativeBaseLpStrategy):
    strategy._position_id = "pos-1"
    strategy._range_center = Decimal("2000")
    strategy._range_lower = Decimal("1800")
    strategy._range_upper = Decimal("2200")
    strategy._last_rebalance_ts = datetime.now(UTC) - timedelta(days=1)
    strategy._last_position_usd = Decimal("100")

    market = make_market(price=Decimal("2020"))
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "IN_RANGE"


def test_holds_when_rebalance_interval_not_elapsed(strategy: ConservativeBaseLpStrategy):
    strategy._position_id = "pos-1"
    strategy._range_center = Decimal("2000")
    strategy._range_lower = Decimal("1800")
    strategy._range_upper = Decimal("2200")
    strategy._last_rebalance_ts = datetime.now(UTC)
    strategy._last_position_usd = Decimal("100")

    market = make_market(price=Decimal("2400"))
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "REBALANCE_COOLDOWN"


def test_closes_lp_when_out_of_range_and_thresholds_met(strategy: ConservativeBaseLpStrategy):
    strategy._position_id = "pos-1"
    strategy._range_center = Decimal("2000")
    strategy._range_lower = Decimal("1800")
    strategy._range_upper = Decimal("2200")
    strategy._last_rebalance_ts = datetime.now(UTC) - timedelta(days=1)
    strategy._last_position_usd = Decimal("100")

    market = make_market(price=Decimal("2400"))
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_CLOSE"
    assert intent.position_id == "WETH/USDC/volatile"


def test_reopen_cooldown_blocks_new_open(strategy: ConservativeBaseLpStrategy):
    strategy._pending_reopen = True
    strategy._last_action_ts = datetime.now(UTC)

    market = make_market()
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "REOPEN_COOLDOWN"


def test_data_unavailable_returns_hold(strategy: ConservativeBaseLpStrategy):
    market = make_market()
    market.price = lambda token: (_ for _ in ()).throw(ValueError("price missing"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "DATA_UNAVAILABLE"


def test_teardown_summary_and_intents(strategy: ConservativeBaseLpStrategy):
    empty_summary = strategy.get_open_positions()
    assert empty_summary.total_value_usd == Decimal("0")
    assert len(empty_summary.positions) == 0

    strategy._position_id = "pos-1"
    strategy._last_position_usd = Decimal("88")

    summary = strategy.get_open_positions()
    assert len(summary.positions) == 1
    assert summary.positions[0].protocol == "aerodrome"

    soft_intents = strategy.generate_teardown_intents(TeardownMode.SOFT)
    hard_intents = strategy.generate_teardown_intents(TeardownMode.HARD)

    assert soft_intents[0].intent_type.value == "LP_CLOSE"
    assert soft_intents[0].position_id == "WETH/USDC/volatile"
    assert soft_intents[1].intent_type.value == "SWAP"
    assert soft_intents[1].max_slippage == Decimal("0.005")
    assert hard_intents[1].max_slippage == Decimal("0.02")


def test_teardown_uses_pool_position_id_after_open_result_returns_address(strategy: ConservativeBaseLpStrategy):
    open_intent = Intent.lp_open(
        pool="WETH/USDC",
        amount0=Decimal("0.01"),
        amount1=Decimal("20"),
        range_lower=Decimal("1800"),
        range_upper=Decimal("2200"),
        protocol="aerodrome",
    )
    strategy.on_intent_executed(
        open_intent,
        success=True,
        result=SimpleNamespace(position_id="0xcdac0d6c6c59727a65f871236188350531885c43"),
    )

    intents = strategy.generate_teardown_intents(TeardownMode.SOFT)
    assert intents[0].intent_type.value == "LP_CLOSE"
    assert intents[0].position_id == "WETH/USDC/volatile"
