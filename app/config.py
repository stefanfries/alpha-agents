from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class DBSettings(BaseModel):
    mongodb_uri: str = ""
    db_name: str = "alpha_agents"
    finance_db_name: str = "finance"


class BrokerSettings(BaseModel):
    client_id: str = ""
    client_secret: str = ""


class FinHubSettings(BaseModel):
    base_url: str = "https://ca-fastapi.yellowwater-786ec0d0.germanywestcentral.azurecontainerapps.io"
    timeout_s: int = 65  # cold start on scale-to-zero can take 30-60 s


class ResearchSettings(BaseModel):
    lookback_days: int = 365


class ScreeningSettings(BaseModel):
    top_n: int = 20
    min_market_cap_eur: int = 500_000_000
    min_adx: int = 20
    lookback_regression: int = 60
    lookback_regression_short: int = 20
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    tsi_fast: int = 13
    tsi_slow: int = 25
    # Selection policies — all enabled ones must pass (AND logic)
    policy_supertrend: bool = True
    policy_ema20_rising: bool = True
    policy_adx: bool = True
    policy_price_above_ema50: bool = True


class WarrantSelectionSettings(BaseModel):
    min_days_to_expiry: int = 270   # 9 months
    max_days_to_expiry: int = 365   # 12 months
    atm_band: float = 0.02          # strike filter: current_price × (1 ± atm_band)
    atm_band_fallback: float = 0.10 # widened band retried when narrow band returns nothing


class PortfolioSettings(BaseModel):
    capital_eur: float = 100_000.0
    sizing_method: str = "equal"       # "equal" | "score_weighted" | "trend_weighted"
    max_position_weight: float = 0.10
    max_positions: int = 20


class RiskSettings(BaseModel):
    max_position_weight: float = 0.10
    max_sector_weight: float = 0.30
    max_positions: int = 30


class ExecutionSettings(BaseModel):
    dry_run: bool = True
    min_trade_eur: float = 1000.0
    order_type: str = "limit"          # "market" | "limit"


class UISettings(BaseModel):
    dark_mode: bool = False


class LogSettings(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    file: str = "alpha_agents.log"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )

    db: DBSettings = DBSettings()
    broker: BrokerSettings = BrokerSettings()
    finhub: FinHubSettings = FinHubSettings()
    research: ResearchSettings = ResearchSettings()
    screening: ScreeningSettings = ScreeningSettings()
    warrant_selection: WarrantSelectionSettings = WarrantSelectionSettings()
    portfolio: PortfolioSettings = PortfolioSettings()
    risk: RiskSettings = RiskSettings()
    execution: ExecutionSettings = ExecutionSettings()
    ui: UISettings = UISettings()
    log: LogSettings = LogSettings()


settings = Settings()
