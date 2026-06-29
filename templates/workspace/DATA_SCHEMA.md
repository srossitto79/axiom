# Data Schema

This document describes every column that may appear on the backtest / scanner
DataFrame. A compact summary (core columns + the always-guard rule + column
categories) is injected into every worker agent's context; this full reference is
read on demand via `read_file` when exact column names, dates, or types are needed.

**Critical rule — always guard column access:**
```python
if 'col' in df.columns:
    ...
```
Columns vary by asset, timeframe, and backtest window. Never assume a column is
present unless you check. Tier B columns are absent on any backtest window that
predates December 2025.

---

## Core Columns (always present)

| Column    | Type    | Description                               |
|-----------|---------|-------------------------------------------|
| timestamp | int64   | Unix milliseconds UTC (candle open time)  |
| open      | float64 | Candle open price                         |
| high      | float64 | Candle high price                         |
| low       | float64 | Candle low price                          |
| close     | float64 | Candle close price                        |
| volume    | float64 | Volume in base currency                   |

---

## Tier A — Enrichment Columns (2023+, available on any meaningful backtest)

Joined via `merge_asof` at enrich time. Available for BTC, ETH, SOL, BNB.
Absent for candles before the indicated "Available From" date.

### Derivatives (collected from Binance, continuous since 2020–2021)

| Column               | Granularity | Available From | Description |
|----------------------|-------------|----------------|-------------|
| funding_rate         | 8h          | 2020-11        | Binance Futures 8-hour funding rate. Positive = longs pay shorts; negative = shorts pay longs. Sentiment / positioning signal. |
| open_interest        | 1h          | 2020-11        | Aggregated perpetual open interest in base currency. Rising OI + rising price = trend confirmation; rising OI + falling price = bearish. |
| long_short_ratio     | 1h          | 2021-01        | Ratio of accounts with net long vs net short positions. >1 = more longs. |
| long_account         | 1h          | 2021-01        | Fraction of accounts that are net long (0–1). |
| short_account        | 1h          | 2021-01        | Fraction of accounts that are net short (0–1). |
| taker_buy_volume     | 1h          | 2021-01        | Taker buy volume in base currency. Aggressive buying pressure. |
| taker_sell_volume    | 1h          | 2021-01        | Taker sell volume in base currency. Aggressive selling pressure. |
| taker_buy_sell_ratio | 1h          | 2021-01        | taker_buy_volume / taker_sell_volume. >1 = buy-side dominance. |

### On-chain & Social (sourced from LAN metrics API, continuous since 2023)

| Column                    | Granularity | Available From | Description |
|---------------------------|-------------|----------------|-------------|
| active_addresses_24h      | 1d          | 2023-01        | Number of unique on-chain addresses active in the past 24h. Proxy for network utilisation and organic demand. |
| daily_active_addresses    | 1d          | 2023-01        | Distinct addresses that sent or received coins in the day. |
| annual_inflation_rate     | 1d          | 2023-01        | Annualised token issuance rate (%). Lower = scarcer supply growth. |
| circulation               | 1d          | 2023-01        | Circulating supply in token units. |
| rank                      | 1d          | 2023-01        | Market cap rank (1 = largest). |
| twitter_followers         | 1d          | 2023-01        | Official project Twitter follower count. Social traction proxy. |
| fully_diluted_valuation_usd | 1d        | 2023-06        | Fully diluted market cap in USD. |
| total_supply              | 1d          | 2023-06        | Total supply including locked / vested tokens. |
| marketcap_usd_change_1d   | 1d          | 2023-06        | 24h percentage change in USD market cap. |
| gini_index                | 1d          | 2024-01        | Gini coefficient of on-chain wealth distribution (0–1). Higher = more concentrated. |
| btc_s_and_p_price_divergence | 1d      | 2024-01        | BTC price divergence from S&P 500 (z-score or ratio). Macro correlation signal. |

### Macro / Sentiment (daily, Axiom native collectors)

These are **opt-in** (`include_macro=True`) and RESEARCH-ONLY. Daily closes carry
same-day lookahead when merged onto intraday candles — never use in live strategies.

| Column          | Granularity | Available From | Description |
|-----------------|-------------|----------------|-------------|
| fear_greed_value | 1d         | 2021-01        | Crypto Fear & Greed index (0–100). 0–25 extreme fear, 75–100 extreme greed. |
| fear_greed_class | 1d         | 2021-01        | Text label: "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed". |
| btc_dominance   | 1d          | 2021-01        | BTC market cap as % of total crypto market cap. |
| vix_close       | 1d          | 2021-01        | CBOE VIX (equity volatility index) daily close. |
| dxy_close       | 1d          | 2021-01        | US Dollar Index daily close. |
| spy_close       | 1d          | 2021-01        | S&P 500 ETF (SPY) daily close price. |
| treasury_10y    | 1d          | 2021-01        | US 10-year treasury yield (%). |

---

## Tier B — Order Book & Liquidation Columns (Dec 2025+, ~6 months of history)

These columns are **absent on any backtest window that ends before 2025-12-01**.
Always guard: `if 'liq_buy_usd' in df.columns:`.
Sourced from the LAN metrics API (L2 order book snapshots + liquidation stream).
Available for BTC, ETH, SOL, BNB.

### Liquidations

| Column          | Granularity | Description |
|-----------------|-------------|-------------|
| liq_buy_count   | 1h          | Number of forced buy (short-squeeze) liquidation orders. |
| liq_sell_count  | 1h          | Number of forced sell (long-liquidation) orders. |
| liq_buy_usd     | 1h          | USD value of short-squeeze liquidations. Spikes = capitulation of shorts. |
| liq_sell_usd    | 1h          | USD value of long liquidations. Spikes = forced deleveraging of longs. |
| liq_delta_usd   | 1h          | liq_buy_usd − liq_sell_usd. Positive = net short squeeze; negative = net long liquidation. |
| liq_total_usd   | 1h          | Total liquidation volume (liq_buy_usd + liq_sell_usd). |
| liq_buy_max_usd | 1h          | Largest single short-squeeze liquidation order in the period (USD). |
| liq_sell_max_usd| 1h          | Largest single long-liquidation order in the period (USD). |

### L2 Order Book Depth

Sampled at native collection frequency (~1h), resampled to strategy timeframe.
Depth buckets: 1% / 5% / 10% from mid-price.

| Column                    | Description |
|---------------------------|-------------|
| l2_ask_volume_1pct        | Resting ask volume within 1% above mid-price (USD). |
| l2_ask_volume_5pct        | Resting ask volume within 5% above mid-price (USD). |
| l2_ask_volume_10pct       | Resting ask volume within 10% above mid-price (USD). |
| l2_bid_volume_1pct        | Resting bid volume within 1% below mid-price (USD). |
| l2_bid_volume_5pct        | Resting bid volume within 5% below mid-price (USD). |
| l2_bid_volume_10pct       | Resting bid volume within 10% below mid-price (USD). |
| l2_bid_ask_ratio_1pct     | l2_bid_volume_1pct / l2_ask_volume_1pct. >1 = more bids than asks near price. |
| l2_bid_ask_ratio_5pct     | Same ratio at 5% depth. |
| l2_bid_ask_ratio_10pct    | Same ratio at 10% depth. |
| l2_imbalance_1pct         | (bid − ask) / (bid + ask) at 1% depth. Range −1 to 1. Positive = buy-side pressure. |
| l2_imbalance_5pct         | Same imbalance at 5% depth. |
| l2_imbalance_10pct        | Same imbalance at 10% depth. |
| l2_mid_price_avg          | Average mid-price during the period. |
| l2_mid_price_std          | Std-dev of mid-price (intrabar volatility proxy). |
| l2_spread_bps_avg         | Average bid-ask spread in basis points. |
| l2_spread_bps_std         | Std-dev of bid-ask spread. Elevated = uncertain liquidity. |
| l2_weighted_mid_price_avg | Volume-weighted mid-price average. |
| l2_weighted_mid_price_std | Std-dev of volume-weighted mid-price. |

### News Sentiment

| Column         | Granularity | Description |
|----------------|-------------|-------------|
| news_sentiment | 4h / 1d     | Aggregated news sentiment score (−1 very negative → +1 very positive). |
| news_volume    | 4h / 1d     | Number of news articles collected in the period. |

---

## Dynamic Column Availability Note

At strategy-generation time you will receive a **"## DATA AVAILABILITY"** section
(scoped to your target assets/timeframes when a hypothesis is in focus, or the
full menu otherwise) listing the exact columns confirmed present and their date
ranges. Use that section — not this schema — to decide which Tier B columns to
reference in your hypothesis code.

When no such section appears, assume only Tier A columns are available.
