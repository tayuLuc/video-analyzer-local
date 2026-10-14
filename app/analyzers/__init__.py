"""
Video analyzers.

One analyzer, one protocol: any server exposing the OpenAI `/v1` API —
LiteLLM proxy, vLLM, llama.cpp, Ollama, LM Studio. Adding a backend is a
config change (OPENAI_BASE_URL), not a new provider class.
"""

from app.analyzers.base import (
    BaseAnalyzer,
    AnalyzerError,
    RateLimitError,
    AuthenticationError,
    VideoProcessingError,
)
from app.analyzers.openai import OpenAIAnalyzer

__all__ = [
    "BaseAnalyzer",
    "OpenAIAnalyzer",
    "AnalyzerError",
    "RateLimitError",
    "AuthenticationError",
    "VideoProcessingError",
]
