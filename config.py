from pydantic_settings import BaseSettings
from pydantic import ConfigDict, field_validator


class Settings(BaseSettings):
    model_config = ConfigDict(env_file=".env", extra="ignore")
    # Discord
    discord_token: str

    # LLM
    llm_provider: str = "openai"          # openai | anthropic | ollama | openrouter
    llm_model: str = "gpt-4o-mini"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434/v1"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Persona
    system_prompt: str = "You are 1812, a helpful and friendly Discord chatbot."

    # History
    max_history_messages: int = 20        # per channel, before summarisation
    summarise_at: int = 40                # trigger summarisation at this count

    # Rate limiting
    rate_limit_requests: int = 10
    rate_limit_window_seconds: int = 60

    # Storage
    db_path: str = "1812.db"

    # Web sidecar
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    @field_validator("llm_provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        allowed = {"openai", "anthropic", "ollama", "openrouter"}
        if v not in allowed:
            raise ValueError(f"llm_provider must be one of {allowed}")
        return v


settings = Settings()
