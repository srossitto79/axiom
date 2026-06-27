"""Z-score mean reversion strategy on any column.

Computes z = (col - rolling_mean) / rolling_std. Configurable column
param (default "close") allows using enrichment columns like
liq_short_volume, l2_imbalance_5_avg, funding_rate, etc.

Enters long when z drops below -entry_z (oversold on that column).
Exits when z crosses back above exit_z.
"""

import pandas as pd

from axiom.strategies.base import BaseStrategy, Signal

TYPE_NAME = "zscore_reversion"


class ZScoreReversionStrategy(BaseStrategy):
    """Z-score mean reversion on a configurable column."""

    @property
    def name(self) -> str:
        col = self.params.get("column", "close")
        return f"Z-Score {col} ({self.asset})"

    @property
    def asset(self) -> str:
        return self.params.get("_asset", "BTC")

    @property
    def strategy_type(self) -> str:
        return TYPE_NAME

    @property
    def default_params(self) -> dict:
        return {
            "column": "close",
            "period": 20,
            "entry_threshold": 2.0,
            "exit_threshold": 0.0,
            "leverage": 1.0,
        }

    @property
    def compatible_regimes(self) -> set[str]:
        return {"RANGE_BOUND", "TREND_DOWN", "TREND_UP"}

    def describe(self) -> str:
        col = self.params.get("column", "close")
        period = int(self.params.get("period", 20))
        entry_t = float(self.params.get("entry_threshold", 2.0))
        exit_t = float(self.params.get("exit_threshold", 0.0))
        return (
            f"Z-score mean reversion on '{col}': {period}-period z-score, "
            f"enter when z < -{entry_t}, exit when z > {exit_t}."
        )

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        col = str(self.params.get("column", "close"))
        period = int(self.params.get("period", 20))
        entry_z = float(self.params.get("entry_threshold", 2.0))
        exit_z = float(self.params.get("exit_threshold", 0.0))

        if col not in df.columns:
            return Signal(
                entry_signal=False, exit_signal=False,
                price=float(df["close"].iloc[-1]) if "close" in df.columns else 0.0,
                direction="long", confidence=0.0,
            )

        curr_val = float(df[col].iloc[-1])
        curr_close = float(df["close"].iloc[-1]) if "close" in df.columns else curr_val

        if len(df) < period + 2:
            return Signal(
                entry_signal=False, exit_signal=False,
                price=round(curr_close, 4), direction="long", confidence=0.0,
            )

        rolling_mean = df[col].rolling(period).mean()
        rolling_std = df[col].rolling(period).std()

        mean_val = rolling_mean.iloc[-1]
        std_val = rolling_std.iloc[-1]

        if pd.isna(mean_val) or pd.isna(std_val) or std_val == 0:
            return Signal(
                entry_signal=False, exit_signal=False,
                price=round(curr_close, 4), direction="long", confidence=0.0,
            )

        z = (curr_val - float(mean_val)) / float(std_val)

        entry = z < -entry_z
        exit_ = z > exit_z

        conf = 0.0
        if entry:
            conf = min(abs(z) / (entry_z * 2), 1.0)

        return Signal(
            entry_signal=bool(entry), exit_signal=bool(exit_),
            price=round(curr_close, 4),
            direction="short" if z > 0 else "long",
            confidence=round(conf, 4),
            indicators={
                "z_score": round(z, 4),
                "rolling_mean": round(float(mean_val), 4),
                "rolling_std": round(float(std_val), 4),
            },
        )

    def parameter_space(self) -> dict:
        return {
            "period": (10, 500, 10),
            "entry_threshold": (1.0, 3.0, 0.5),
            "exit_threshold": (0.0, 1.0, 0.5),
        }


STRATEGY_CLASS = ZScoreReversionStrategy

STRATEGIES = [
    ("PREBUILT-ZSCORE", ZScoreReversionStrategy, {"_asset": "BTC"}),
]
