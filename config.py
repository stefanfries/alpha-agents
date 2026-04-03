from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Comdirect broker credentials
    comdirect_client_id: str = ""
    comdirect_client_secret: str = ""

    # Research
    research_lookback_days: int = 365

    # Screening
    screening_top_n: int = 20
    min_market_cap_eur: int = 500_000_000

    # Portfolio construction
    portfolio_capital_eur: float = 10_000.0
    sizing_method: str = "equal"          # "equal" | "score_weighted" | "vol_scaled"
    max_position_weight: float = 0.10

    # Risk
    risk_max_position_weight: float = 0.10
    risk_max_sector_weight: float = 0.30
    risk_max_positions: int = 30

    # Execution
    execution_dry_run: bool = True
    execution_min_trade_eur: float = 100.0
    execution_order_type: str = "limit"   # "market" | "limit"


settings = Settings()
