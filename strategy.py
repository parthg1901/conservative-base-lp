import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Optional

from almanak.framework.intents import Intent
from almanak.framework.strategies import IntentStrategy, MarketSnapshot, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="conservative_base_lp",
    description="Conservative small-cap Aerodrome LP strategy on Base",
    version="1.0.0",
    author="Almanak",
    tags=["lp", "aerodrome", "base", "conservative", "risk-managed"],
    supported_chains=["base"],
    supported_protocols=["aerodrome"],
    intent_types=["LP_OPEN", "LP_CLOSE", "HOLD", "SWAP"],
    default_chain="base",
)
class ConservativeBaseLpStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def get_config(key: str, default: Any) -> Any:
            if isinstance(self.config, dict):
                return self.config.get(key, default)
            return getattr(self.config, key, default)

        configured_pool = str(get_config("pool", "WETH/USDC"))
        pool_parts = [part.strip() for part in configured_pool.split("/") if part.strip()]
        token0 = pool_parts[0] if len(pool_parts) > 0 else "WETH"
        token1 = pool_parts[1] if len(pool_parts) > 1 else "USDC"
        pool_type = pool_parts[2].lower() if len(pool_parts) > 2 else "volatile"

        self.pool = f"{token0}/{token1}"
        self.pool_type = "stable" if pool_type == "stable" else "volatile"
        self.intent_pool = f"{self.pool}/stable" if self.pool_type == "stable" else self.pool
        self.close_position_id = f"{self.pool}/{self.pool_type}"

        self.protocol = str(get_config("protocol", "aerodrome"))
        self.base_token = str(get_config("base_token", token0))
        self.quote_token = str(get_config("quote_token", token1))

        self.range_width_pct = Decimal(str(get_config("range_width_pct", "20")))
        self.rebalance_drift_trigger_pct = Decimal(str(get_config("rebalance_drift_trigger_pct", "0.08")))
        self.min_rebalance_interval_minutes = int(get_config("min_rebalance_interval_minutes", 720))
        self.reopen_cooldown_minutes = int(get_config("reopen_cooldown_minutes", 180))
        self.min_price_move_to_act_pct = Decimal(str(get_config("min_price_move_to_act_pct", "0.015")))

        self.min_rebalance_notional_usd = Decimal(str(get_config("min_rebalance_notional_usd", "40")))
        self.min_open_notional_usd = Decimal(str(get_config("min_open_notional_usd", "50")))
        self.min_leg_usd = Decimal(str(get_config("min_leg_usd", "20")))
        self.min_total_capital_usd = Decimal(str(get_config("min_total_capital_usd", "80")))
        self.cash_reserve_usd = Decimal(str(get_config("cash_reserve_usd", "15")))
        self.deploy_fraction = Decimal(str(get_config("deploy_fraction", "0.85")))

        self.atr_period = int(get_config("atr_period", 14))
        self.atr_timeframe = str(get_config("atr_timeframe", "1h"))
        self.atr_emergency_pct = Decimal(str(get_config("atr_emergency_pct", "0.035")))
        self.bb_period = int(get_config("bb_period", 20))
        self.bb_std_dev = float(get_config("bb_std_dev", 2.0))
        self.bb_timeframe = str(get_config("bb_timeframe", "1h"))
        self.bb_bandwidth_emergency = Decimal(str(get_config("bb_bandwidth_emergency", "0.09")))
        self.high_vol_confirm_ticks = int(get_config("high_vol_confirm_ticks", 2))

        self.teardown_soft_slippage = Decimal(str(get_config("teardown_soft_slippage", "0.005")))
        self.teardown_hard_slippage = Decimal(str(get_config("teardown_hard_slippage", "0.02")))

        self._position_id: Optional[str] = None
        self._range_lower: Optional[Decimal] = None
        self._range_upper: Optional[Decimal] = None
        self._range_center: Optional[Decimal] = None
        self._last_rebalance_ts: Optional[datetime] = None
        self._last_action_ts: Optional[datetime] = None
        self._pending_reopen: bool = False
        self._high_vol_streak: int = 0
        self._last_position_usd: Decimal = Decimal("0")

    def supports_teardown(self) -> bool:
        return True

    def _now(self, market: Optional[MarketSnapshot] = None) -> datetime:
        ts = getattr(market, "timestamp", None) if market is not None else None
        if isinstance(ts, datetime):
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        return datetime.now(UTC)

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        return Decimal(str(value))

    def _extract_indicator_value(self, indicator: Any, *attrs: str) -> Decimal:
        for attr in attrs:
            if hasattr(indicator, attr):
                return self._to_decimal(getattr(indicator, attr))
        return self._to_decimal(indicator)

    def _minutes_elapsed(self, since: Optional[datetime], now: datetime) -> float:
        if since is None:
            return float("inf")
        return (now - since).total_seconds() / 60.0

    def _build_range(self, price: Decimal) -> tuple[Decimal, Decimal]:
        half_ratio = self.range_width_pct / Decimal("200")
        lower = price * (Decimal("1") - half_ratio)
        upper = price * (Decimal("1") + half_ratio)
        return lower, upper

    def _estimate_position_usd(self, fallback: Decimal = Decimal("0")) -> Decimal:
        if self._last_position_usd > 0:
            return self._last_position_usd
        return fallback

    def _resolve_lp_close_position_id(self) -> str:
        if self.protocol.lower() != "aerodrome":
            return str(self._position_id)
        return self.close_position_id

    def decide(self, market: MarketSnapshot) -> Optional[Intent]:
        now = self._now(market)
        try:
            price = self._to_decimal(market.price(self.base_token))
            atr_data = market.atr(self.base_token, period=self.atr_period, timeframe=self.atr_timeframe)
            bb_data = market.bollinger_bands(
                self.base_token,
                period=self.bb_period,
                std_dev=self.bb_std_dev,
                timeframe=self.bb_timeframe,
            )
        except Exception as exc:
            return Intent.hold(
                reason=f"Required market data unavailable: {exc}",
                reason_code="DATA_UNAVAILABLE",
            )

        try:
            atr_value = self._extract_indicator_value(atr_data, "value", "atr")
            bb_bandwidth = self._extract_indicator_value(bb_data, "bandwidth")
        except Exception as exc:
            return Intent.hold(
                reason=f"Indicator parse error: {exc}",
                reason_code="INDICATOR_PARSE_ERROR",
            )

        atr_pct = Decimal("0") if price <= 0 else atr_value / price
        high_vol = atr_pct >= self.atr_emergency_pct or bb_bandwidth >= self.bb_bandwidth_emergency
        self._high_vol_streak = self._high_vol_streak + 1 if high_vol else 0
        if self._high_vol_streak >= self.high_vol_confirm_ticks:
            return Intent.hold(
                reason="High volatility emergency hold",
                reason_code="EMERGENCY_VOL_HOLD",
                reason_details={
                    "atr_pct": str(atr_pct),
                    "bb_bandwidth": str(bb_bandwidth),
                },
            )

        if self._position_id is not None:
            if self._range_center is None:
                self._range_center = price
            if self._range_lower is None or self._range_upper is None:
                self._range_lower, self._range_upper = self._build_range(price)

            elapsed = self._minutes_elapsed(self._last_rebalance_ts, now)
            if elapsed < self.min_rebalance_interval_minutes:
                return Intent.hold(
                    reason="Rebalance interval not reached",
                    reason_code="REBALANCE_COOLDOWN",
                )

            center = self._range_center if self._range_center and self._range_center > 0 else price
            drift_pct = abs(price - center) / center if center > 0 else Decimal("0")
            out_of_range = bool(self._range_lower and price <= self._range_lower) or bool(
                self._range_upper and price >= self._range_upper
            )

            if not (out_of_range or drift_pct >= self.rebalance_drift_trigger_pct):
                return Intent.hold(reason="LP position within conservative range", reason_code="IN_RANGE")

            if drift_pct < self.min_price_move_to_act_pct:
                return Intent.hold(reason="Move below minimum action threshold", reason_code="MIN_ACTION_FILTER")

            estimated_notional = self._estimate_position_usd()
            if estimated_notional < self.min_rebalance_notional_usd:
                return Intent.hold(
                    reason="Position notional below rebalance threshold",
                    reason_code="MIN_REBALANCE_NOTIONAL",
                )

            self._pending_reopen = True
            self._last_action_ts = now
            return Intent.lp_close(
                position_id=self._resolve_lp_close_position_id(),
                pool=self.intent_pool,
                collect_fees=True,
                protocol=self.protocol,
            )
        try:
            base_balance = market.balance(self.base_token)
            quote_balance = market.balance(self.quote_token)
            base_usd = self._to_decimal(base_balance.balance_usd)
            quote_usd = self._to_decimal(quote_balance.balance_usd)
        except Exception as exc:
            return Intent.hold(reason=f"Balance check failed: {exc}", reason_code="BALANCE_UNAVAILABLE")

        if self._pending_reopen:
            elapsed_since_action = self._minutes_elapsed(self._last_action_ts, now)
            if elapsed_since_action < self.reopen_cooldown_minutes:
                return Intent.hold(reason="Waiting for reopen cooldown", reason_code="REOPEN_COOLDOWN")

        total_usd = base_usd + quote_usd
        if total_usd < self.min_total_capital_usd:
            return Intent.hold(reason="Insufficient total capital", reason_code="INSUFFICIENT_CAPITAL")

        deployable_usd = max(Decimal("0"), total_usd - self.cash_reserve_usd)
        deployable_usd = deployable_usd * self.deploy_fraction
        if deployable_usd < self.min_open_notional_usd:
            return Intent.hold(reason="Deployable capital below open threshold", reason_code="MIN_OPEN_NOTIONAL")

        leg_usd = deployable_usd / Decimal("2")
        if leg_usd < self.min_leg_usd:
            return Intent.hold(reason="Each LP leg below minimum size", reason_code="MIN_LEG_USD")

        if price <= 0:
            return Intent.hold(reason="Invalid price for LP sizing", reason_code="INVALID_PRICE")

        amount0 = leg_usd / price
        amount1 = leg_usd
        if amount0 <= 0 or amount1 <= 0:
            return Intent.hold(reason="Calculated LP amount is zero", reason_code="ZERO_AMOUNT")

        lower, upper = self._build_range(price)
        self._range_lower = lower
        self._range_upper = upper
        self._range_center = price
        self._last_action_ts = now
        self._pending_reopen = False
        self._last_position_usd = deployable_usd

        return Intent.lp_open(
            pool=self.intent_pool,
            amount0=amount0,
            amount1=amount1,
            range_lower=lower,
            range_upper=upper,
            protocol=self.protocol,
        )

    def on_intent_executed(self, intent, success: bool, result):
        if not success:
            return

        now = datetime.now(UTC)
        intent_type = getattr(getattr(intent, "intent_type", None), "value", "")
        if intent_type == "LP_OPEN":
            self._position_id = getattr(result, "position_id", None) or self.intent_pool
            self._last_rebalance_ts = now
            self._pending_reopen = False
        elif intent_type == "LP_CLOSE":
            self._position_id = None
            self._range_lower = None
            self._range_upper = None
            self._range_center = None
            self._last_rebalance_ts = now
            self._last_position_usd = Decimal("0")

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "conservative_base_lp",
            "chain": self.chain,
            "protocol": self.protocol,
            "pool": self.pool,
            "position_id": self._position_id,
            "pending_reopen": self._pending_reopen,
            "high_vol_streak": self._high_vol_streak,
        }

    def get_persistent_state(self):
        return {
            "position_id": self._position_id,
            "range_lower": str(self._range_lower) if self._range_lower is not None else None,
            "range_upper": str(self._range_upper) if self._range_upper is not None else None,
            "range_center": str(self._range_center) if self._range_center is not None else None,
            "last_rebalance_ts": self._last_rebalance_ts.isoformat() if self._last_rebalance_ts else None,
            "last_action_ts": self._last_action_ts.isoformat() if self._last_action_ts else None,
            "pending_reopen": self._pending_reopen,
            "high_vol_streak": self._high_vol_streak,
            "last_position_usd": str(self._last_position_usd),
        }

    def load_persistent_state(self, state):
        if not state:
            return

        self._position_id = state.get("position_id")
        self._range_lower = Decimal(state["range_lower"]) if state.get("range_lower") else None
        self._range_upper = Decimal(state["range_upper"]) if state.get("range_upper") else None
        self._range_center = Decimal(state["range_center"]) if state.get("range_center") else None
        self._last_rebalance_ts = (
            datetime.fromisoformat(state["last_rebalance_ts"]) if state.get("last_rebalance_ts") else None
        )
        self._last_action_ts = datetime.fromisoformat(state["last_action_ts"]) if state.get("last_action_ts") else None
        self._pending_reopen = bool(state.get("pending_reopen", False))
        self._high_vol_streak = int(state.get("high_vol_streak", 0))
        self._last_position_usd = Decimal(str(state.get("last_position_usd", "0")))

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions = []
        if self._position_id is not None:
            value = self._estimate_position_usd()
            positions.append(
                PositionInfo(
                    position_type=PositionType.LP,
                    position_id=self._resolve_lp_close_position_id(),
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=value,
                    details={
                        "pool": self.pool,
                        "range_lower": str(self._range_lower) if self._range_lower is not None else None,
                        "range_upper": str(self._range_upper) if self._range_upper is not None else None,
                    },
                )
            )

        total_value = sum((p.value_usd for p in positions), Decimal("0"))
        return TeardownPositionSummary(
            strategy_id=getattr(self, "strategy_id", "conservative_base_lp"),
            timestamp=datetime.now(UTC),
            total_value_usd=total_value,
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        from almanak.framework.teardown import TeardownMode

        intents: list[Intent] = []
        max_slippage = self.teardown_hard_slippage if mode == TeardownMode.HARD else self.teardown_soft_slippage

        if self._position_id is not None:
            intents.append(
                Intent.lp_close(
                    position_id=self._resolve_lp_close_position_id(),
                    pool=self.intent_pool,
                    collect_fees=True,
                    protocol=self.protocol,
                )
            )

        intents.append(
            Intent.swap(
                from_token=self.base_token,
                to_token=self.quote_token,
                amount="all",
                max_slippage=max_slippage,
                protocol=self.protocol,
            )
        )

        return intents
