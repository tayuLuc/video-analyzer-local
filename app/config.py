"""
Configuration for the video analyzer.

One backend: any OpenAI-compatible `/v1` server — LiteLLM proxy, vLLM,
llama.cpp, Ollama, LM Studio. The model list is read from that server at
runtime; nothing is enumerated here.
"""

import os
from typing import Optional
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# Applied when OPENAI_BASE_URL is unset. `host.docker.internal` is the usual
# case: the container reaches a server on the Docker host by that name.
DEFAULT_BASE_URL = "http://host.docker.internal:4000/v1"


class Settings(BaseModel):
    """Application settings with defaults from environment variables."""

    # OpenAI-compatible endpoint
    openai_base_url: str = DEFAULT_BASE_URL
    openai_api_key: Optional[str] = None  # local servers need none
    openai_model_name: str = ""

    # Processing settings
    chunk_duration_minutes: int = 10
    # 0 means "no limit"; the upload endpoint treats it as unlimited.
    max_upload_size_mb: int = 0
    chunk_split_mode: str = "fixed"  # 'fixed' or 'silence_aware'
    silence_window_seconds: int = 120
    silence_min_duration_seconds: float = 3.0
    silence_noise_db: float = -35.0

    # Paths
    upload_directory: str = "uploads"
    output_directory: str = "outputs"
    temp_directory: str = "temporary"
    prompts_directory: str = "prompts"


class ConfigManager:
    """
    Manages runtime configuration.
    Allows updating settings via web interface without restart.
    """

    def __init__(self):
        self._settings = self._load_from_env()
        self._runtime_api_key: Optional[str] = None

    def _load_from_env(self) -> Settings:
        """Load settings from environment variables."""
        return Settings(
            openai_base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model_name=os.getenv("OPENAI_MODEL_NAME", ""),
            chunk_duration_minutes=int(os.getenv("CHUNK_DURATION_MINUTES", "10")),
            max_upload_size_mb=int(os.getenv("MAX_UPLOAD_SIZE_MB", "0")),
            chunk_split_mode=os.getenv("CHUNK_SPLIT_MODE", "fixed"),
            silence_window_seconds=int(os.getenv("SILENCE_WINDOW_SECONDS", "120")),
            silence_min_duration_seconds=float(os.getenv("SILENCE_MIN_DURATION_SECONDS", "3.0")),
            silence_noise_db=float(os.getenv("SILENCE_NOISE_DB", "-35.0")),
            upload_directory=os.getenv("UPLOAD_DIRECTORY", "uploads"),
            output_directory=os.getenv("OUTPUT_DIRECTORY", "outputs"),
            temp_directory=os.getenv("TEMP_DIRECTORY", "temporary"),
            prompts_directory=os.getenv("PROMPTS_DIRECTORY", "prompts"),
        )

    @property
    def settings(self) -> Settings:
        return self._settings

    def get_api_key(self) -> Optional[str]:
        """Runtime key wins over the environment."""
        return self._runtime_api_key or self._settings.openai_api_key

    def set_runtime_api_key(self, api_key: str):
        """Set the API key at runtime (from web interface)."""
        self._runtime_api_key = api_key

    def has_valid_api_key(self) -> bool:
        """Whether a key is present. Local servers do not need one."""
        key = self.get_api_key()
        return key is not None and len(key) > 10

    def update_settings(self, **kwargs):
        """Update settings at runtime."""
        current_dict = self._settings.model_dump()
        current_dict.update(kwargs)
        self._settings = Settings(**current_dict)


# Global config manager instance
config_manager = ConfigManager()