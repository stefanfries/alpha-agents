from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator


class Ticker(BaseModel):
    symbol: str
    isin: str | None = None
    exchange: str | None = None
    name: str | None = None

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        return v.upper()


class OHLCV(BaseModel):
    ticker: Ticker
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class Position(BaseModel):
    ticker: Ticker
    quantity: Decimal
    avg_cost: Decimal


class Order(BaseModel):
    ticker: Ticker
    side: Literal["buy", "sell"]
    quantity: Decimal | None = None
    notional_eur: Decimal | None = None
    order_type: Literal["market", "limit"]
    limit_price: Decimal | None = None

    @model_validator(mode="after")
    def validate_amount_by_side(self) -> "Order":
        if self.side == "buy" and (self.notional_eur is None or self.quantity is not None):
            raise ValueError("Buy orders require notional_eur and must not set quantity")
        if self.side == "sell" and (self.quantity is None or self.notional_eur is not None):
            raise ValueError("Sell orders require quantity and must not set notional_eur")
        return self
