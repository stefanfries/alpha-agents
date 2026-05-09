<!-- markdownlint-configure-file {"MD024": {"siblings_only": true}} -->

# WARRANT BLUEPRINT (STRATEGY-BASED)

## Overview

This document defines a structured framework for selecting warrants
based on strategy type, using delta, leverage, maturity, moneyness, and
premium constraints.

------------------------------------------------------------------------

## 1. Core Trend Following (Main Strategy)

### Objective

Capture sustained multi-week to multi-month trends with stable leverage
and controlled decay.

### Target Profile

- Delta: 0.60 -- 0.75\
- Leverage: 4 -- 7\
- Maturity: 12 -- 30 months (ideal 18--24 months)\
- Moneyness: 0.95 -- 1.10 (slightly ITM)\
- Premium p.a.: \< 18%

### Behaviour

- Tracks underlying efficiently
- Moderate convexity
- Low decay sensitivity
- Suitable for swing and positional trading

------------------------------------------------------------------------

## 2. Momentum / Acceleration Trades

### Objective

Capture breakout phases and strong directional moves.

### Target Profile

- Delta: 0.50 -- 0.65\
- Leverage: 6 -- 10\
- Maturity: 6 -- 18 months\
- Moneyness: 0.90 -- 1.05 (ATM to slightly OTM)\
- Premium p.a.: \< 15%

### Behaviour

- High convexity
- Fast reaction to price moves
- Higher volatility sensitivity
- Requires active monitoring

------------------------------------------------------------------------

## 3. Defensive Trend / Core Hold

### Objective

Stable exposure with reduced volatility and drawdown resilience.

### Target Profile

- Delta: 0.70 -- 0.85\
- Leverage: 3 -- 5\
- Maturity: 18 -- 36 months\
- Moneyness: 1.05 -- 1.25 (ITM)\
- Premium p.a.: \< 20%

### Behaviour

- Behaves like leveraged equity exposure
- Low decay pressure
- Suitable for core portfolio holdings

------------------------------------------------------------------------

## 4. Tactical / High Conviction Trades

### Objective

Short-to-medium term asymmetric payoff opportunities.

### Target Profile

- Delta: 0.40 -- 0.60\
- Leverage: 8 -- 15\
- Maturity: 3 -- 12 months\
- Moneyness: ATM or slightly OTM\
- Premium p.a.: \< 12--15%

### Behaviour

- High convexity
- Strong sensitivity to timing
- Requires disciplined exit strategy

------------------------------------------------------------------------

## 5. Instruments to Avoid

### Characteristics to avoid

- Leverage \> 20\
- Delta \< 0.3\
- Missing or incomplete Greeks\
- Deep OTM structures with high decay\
- Opaque pricing or inconsistent spreads

### Reason

These behave like volatility bets rather than structured trend
instruments.

------------------------------------------------------------------------

## 6. Decision Matrix

  Strategy          Delta        Leverage   Maturity   Role
  --------------- | ---------- | -------- | -------- | --------------
  Core Trend        0.60--0.75   4--7       18--24m    Main exposure
  Momentum          0.50--0.65   6--10      6--18m     Acceleration
  Defensive Trend   0.70--0.85   3--5       24--36m    Stability
  Tactical Bet      0.40--0.60   8--15      3--12m     Asymmetric

------------------------------------------------------------------------

## 7. Key Principle

Delta selection must align with investment horizon:

- Long trend → Delta \~0.65\
- Breakout → Delta \~0.55\
- Core hold → Delta \~0.75

This alignment is more important than leverage alone.
