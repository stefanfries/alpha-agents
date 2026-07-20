from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AmountValue(BaseModel):
    """Monetary or quantity value with currency/unit.

    Mirrors the storage format used by comdirect_api:
    value is serialised as str(Decimal) so that cross-depot queries against
    finance.depot_snapshots and virtual_depot_snapshots are structurally uniform.
    """

    value: str | None = None  # str(Decimal), e.g. "100.0000"
    unit: str | None = None   # e.g. "EUR" or "ST" (pieces)


class PriceAmountValue(AmountValue):
    """Price amount with optional quote timestamp."""

    price_datetime: str | None = None


class QuantSystem(BaseModel):
    quant_system_id: str
    name: str
    depot_id: str
    depot_type: Literal["real", "virtual"]
    indices: list[str]
    capital_eur: float
    status: Literal["draft", "active", "paused", "archived"] = "draft"
    config_overrides: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class VirtualDepot(BaseModel):
    depot_id: str
    name: str
    starting_capital: float = 100_000.0
    created_at: datetime
    updated_at: datetime


class VirtualDepotPosition(BaseModel):
    position_id: str = ""
    wkn: str | None = None
    isin: str | None = None
    instrument_name: str | None = None
    quantity: AmountValue = Field(default_factory=AmountValue)
    current_price: PriceAmountValue = Field(default_factory=PriceAmountValue)
    current_value: AmountValue = Field(default_factory=AmountValue)
    average_purchase_price: AmountValue = Field(default_factory=AmountValue)
    held_since_date: str | None = None
    purchase_price_at_entry: AmountValue = Field(default_factory=AmountValue)


class VirtualDepotSnapshot(BaseModel):
    depot_id: str
    current_cash: float
    positions: list[VirtualDepotPosition] = Field(default_factory=list)
    recorded_at: datetime
    triggered_by: str  # execution_id


class VirtualDepotTransaction(BaseModel):
    transaction_id: str
    depot_id: str
    execution_id: str
    wkn: str
    transaction_type: Literal["BUY", "SELL"]
    quantity: str | None = None        # str(Decimal), e.g. "50.0000"
    quantity_unit: str | None = None   # e.g. "ST"
    execution_price: str | None = None # str(Decimal), EUR per unit
    price_unit: str | None = None      # e.g. "EUR"
    transaction_value: str | None = None  # str(Decimal), quantity × execution_price
    booking_date: datetime
    recorded_at: datetime
