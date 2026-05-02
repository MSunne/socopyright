from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-chat"
    LLM_MAX_CONCURRENCY: int = 6

    JWT_SECRET: str = "dev-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 168
    ADMIN_TOKEN: str = "dev-admin-token-change-me"

    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/app.db"
    DATA_DIR: str = "./data/generated"

    BROWSER_MAX_CONCURRENCY: int = 3

    JOB_CONCURRENCY: int = 2


settings = Settings()
