import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

    # API Configuration
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "ProMeet AI Service"
    
    # CORS
    BACKEND_CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    # AI Providers
    OPENAI_API_KEY: str = Field(default="", description="OpenAI API Key")
    GOOGLE_API_KEY: str = Field(default="", description="Google Gemini API Key")
    GEMINI_API_KEY: str = Field(default="", description="Alternate Gemini API Key")

    # Vector DB
    # Defaulting to local weaviate in docker
    WEAVIATE_URL: str = Field(default="http://localhost:8080", description="Weaviate URL")
    
    # Internal Backend API
    API_BASE_URL: str = Field(default="http://localhost:8000/api", description="Base URL for main backend API")
    
    # Email Settings
    EMAIL_SENDER: str = Field(default="", description="Email address for sending notifications")
    EMAIL_PASSWORD: str = Field(default="", description="App password for email sender")
    
    @property
    def google_key(self) -> str:
        """Helper to get whichever Google key is set"""
        return self.GOOGLE_API_KEY or self.GEMINI_API_KEY

settings = Settings()
