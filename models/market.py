from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, field_validator


class Ticker(BaseModel):
    symbol: str
    exchange: str | None = None

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
    quantity: Decimal
    order_type: Literal["market", "limit"]
    limit_price: Decimal | None = None
