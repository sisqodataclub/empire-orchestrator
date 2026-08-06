from pydantic_settings import BaseSettings
from pydantic import ConfigDict

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./tenants.db"
    WORKSPACE_ROOT: str = "./data/workspaces"

    model_config = ConfigDict(extra="ignore", env_file=".env", env_file_encoding="utf-8")

settings = Settings()
