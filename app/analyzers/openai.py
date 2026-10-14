"""
OpenAI-compatible video analyzer.

Talks to anything that speaks the OpenAI `/v1` API: Ollama, vLLM,
llama.cpp, LM Studio. The model list is whatever the server reports —
nothing is hardcoded here.
"""

import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import (
    APIError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError as OpenAIRateLimitError,
)
from openai import AuthenticationError as OpenAIAuthError

from app.analyzers.base import (
    AuthenticationError,
    AnalyzerError,
    BaseAnalyzer,
    RateLimitError,
    VideoProcessingError,
)
from app.config import config_manager
from app.prompts import prompt_manager

logger = logging.getLogger(__name__)

# Same output budget the other OpenAI-compatible backend uses. The
# lossless prompt mode does not fit in 4k.
MAX_TOKENS = 16000

# Sampling points for the vision path, as fractions of chunk duration.
FRAME_FRACTIONS = (0.25, 0.5, 0.75)

# ffmpeg -q:v scale for JPEG quality (2 = high).
FRAME_QUALITY = "2"


class OpenAIAnalyzer(BaseAnalyzer):
    """Video analyzer for any OpenAI-compatible `/v1` server."""

    def __init__(
        self,
        model_name: str = "llava",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        super().__init__(model_name, api_key)
        self.base_url = base_url or config_manager.get_openai_base_url()
        self._client: Optional[AsyncOpenAI] = None

    # ---------- credentials ----------

    def _get_api_key(self) -> str:
        """Get the API key. Local servers do not need one."""
        key = self.api_key or config_manager.get_api_key("openai")
        if not key and not self._is_local_server():
            raise AuthenticationError("OpenAI API key not configured")
        return key or "not-needed-for-local"

    def _is_local_server(self) -> bool:
        """Whether the configured endpoint looks like a local server."""
        return "localhost" in self.base_url or "127.0.0.1" in self.base_url

    def _get_client(self) -> AsyncOpenAI:
        """Get or create the client."""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._get_api_key(),
                base_url=self.base_url,
                max_retries=0,  # retries live in BaseAnalyzer
            )
        return self._client

    # ---------- frames ----------

    @staticmethod
    def _probe_duration(video_path: str) -> float:
        """Duration in seconds via ffprobe."""
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            video_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise VideoProcessingError(
                f"ffprobe failed for {video_path}: {result.stderr.strip()}"
            )
        try:
            return float(json.loads(result.stdout)["format"]["duration"])
        except (KeyError, ValueError, TypeError) as e:
            raise VideoProcessingError(
                f"ffprobe returned no usable duration for {video_path}: {e}"
            ) from e

    @staticmethod
    def _grab_frame(video_path: str, timestamp: float, frame_path: str) -> bool:
        """Extract one frame at `timestamp` seconds."""
        cmd = [
            "ffmpeg",
            "-ss", f"{timestamp:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", FRAME_QUALITY,
            frame_path,
            "-y",
        ]
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0 and os.path.exists(frame_path)

    def _extract_video_frames(self, video_path: str) -> List[str]:
        """Sample frames from a video, base64-encoded as JPEG.

        Blocking: ffmpeg runs in a subprocess, so callers must go through
        `asyncio.to_thread`.
        """
        if not os.path.exists(video_path):
            raise VideoProcessingError(f"Video file not found: {video_path}")

        duration = self._probe_duration(video_path)
        frames: List[str] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            for index, fraction in enumerate(FRAME_FRACTIONS):
                frame_path = os.path.join(temp_dir, f"frame_{index:03d}.jpg")
                timestamp = duration * fraction
                if self._grab_frame(video_path, timestamp, frame_path):
                    encoded = base64.b64encode(
                        Path(frame_path).read_bytes()
                    ).decode("ascii")
                    frames.append(encoded)
                    logger.info(
                        "frame %d/%d at %.1fs -> %d base64 chars",
                        index + 1, len(FRAME_FRACTIONS), timestamp, len(encoded),
                    )
                else:
                    logger.warning(
                        "failed to extract frame at %.1fs", timestamp
                    )

        if not frames:
            raise VideoProcessingError(
                f"ffmpeg produced no frames for {video_path}; "
                "the file may be corrupt or use an unsupported codec"
            )
        return frames

    # ---------- analysis ----------

    async def analyze_video(
        self, video_path: str, prompt: str, chunk_info: Optional[Dict] = None
    ) -> str:
        """Analyze a video chunk and return the model's text."""
        logger.info("analyzing %s via %s (%s)", video_path, self.base_url, self.model_name)
        client = self._get_client()

        # This endpoint cannot take a video file, so it gets sampled frames.
        frame_images = await asyncio.to_thread(self._extract_video_frames, video_path)

        content: List[Dict[str, Any]] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{frame}",
                    "detail": "high",
                },
            }
            for frame in frame_images
        ]
        content.append(
            {"type": "text", "text": prompt_manager.format_chunk_prompt(prompt, chunk_info)}
        )

        try:
            response = await client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": content}],
                max_tokens=MAX_TOKENS,
                temperature=0.1,
            )
        except OpenAIAuthError as e:
            raise AuthenticationError(f"Authentication failed: {e}") from e
        except OpenAIRateLimitError as e:
            raise RateLimitError(f"Rate limit exceeded: {e}") from e
        except APIError as e:
            raise AnalyzerError(f"OpenAI API error: {e}") from e
        except OpenAIError as e:
            raise AnalyzerError(f"OpenAI error: {e}") from e

        return response.choices[0].message.content or ""

    async def get_available_models(self) -> List[Dict[str, Any]]:
        """List the models the configured server reports."""
        try:
            client = self._get_client()
            models_response = await client.models.list()
        except OpenAIAuthError as e:
            raise AuthenticationError(f"Authentication failed: {e}") from e
        except OpenAIError as e:
            raise AnalyzerError(f"Failed to fetch models: {e}") from e

        models = [
            {
                "id": model.id,
                "name": model.id,
                "description": getattr(model, "description", "") or "",
                "supports_video": True,
            }
            for model in models_response.data
        ]
        if not models:
            raise AnalyzerError(
                f"{self.base_url} reports no models; is the server running "
                "and is a model pulled?"
            )
        return models

    def validate_api_key(self) -> bool:
        """Format check only (deprecated, use validate_api_key_async)."""
        try:
            if self._is_local_server():
                return True
            api_key = self._get_api_key()
            return api_key.startswith("sk-") and len(api_key) > 20
        except AnalyzerError:
            return False

    async def validate_api_key_async(self) -> bool:
        """Validate by listing models on the configured server."""
        try:
            await self._get_client().models.list()
            return True
        except AuthenticationError:
            return False
        except OpenAIError:
            # Unreachable server is not a key problem, but it is a
            # configuration problem — report it as invalid.
            return False

    async def combine_analyses(self, analyses: List[str], combine_prompt: str) -> str:
        """Combine chunk analyses into one document."""
        combined_text = "\n\n---\n\n".join(
            f"=== Chunk {i + 1} Analysis ===\n{analysis}"
            for i, analysis in enumerate(analyses)
        )

        try:
            response = await self._get_client().chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "user", "content": f"{combine_prompt}\n\n{combined_text}"}
                ],
                max_tokens=MAX_TOKENS,
                temperature=0.1,
            )
        except OpenAIAuthError as e:
            raise AuthenticationError(f"Authentication failed: {e}") from e
        except OpenAIRateLimitError as e:
            raise RateLimitError(f"Rate limit exceeded: {e}") from e
        except APIError as e:
            raise AnalyzerError(f"Failed to combine analyses: {e}") from e
        except OpenAIError as e:
            raise AnalyzerError(f"Failed to combine analyses: {e}") from e

        return response.choices[0].message.content or ""

    async def close(self):
        """Close the client connection."""
        if self._client is not None:
            await self._client.close()
            self._client = None