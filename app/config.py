"""
Configuration: reads credentials and settings from environment variables.

Locally these come from the .env file. On Railway they come from the
platform's Variables tab (set these in the Railway dashboard, NOT in the
.env file which shouldn't be deployed).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    digitalray_email: str
    digitalray_password: str

    api_secret_key: str = ""
    port: int = 8000

    # The digitalray.ai login page - this is where Playwright starts
    login_page_url: str = "https://www.digitalray.ai/login"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
