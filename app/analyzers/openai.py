"""
OpenAI video analyzer supporting local and cloud APIs.
Supports OpenAI-compatible local servers (Ollama, LocalAI, etc.).
"""

import os
import base64
import asyncio
from typing import Dict, List, Optional, Any
from openai import AsyncOpenAI, OpenAIError

from app.analyzers.base import (
    BaseAnalyzer,
    AnalyzerError,
    RateLimitError,
    AuthenticationError,
    VideoProcessingError,
)
from app.config import config_manager


class OpenAIAnalyzer(BaseAnalyzer):
    """Video analyzer using OpenAI API (supports local servers)."""

    # Default base URL for local OpenAI-compatible servers
    DEFAULT_BASE_URL = "http://localhost:8080/v1"  # llama.cpp server default

    # Default models for local servers
    DEFAULT_MODELS = []

    def __init__(
        self,
        model_name: str = "llava",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        super().__init__(model_name, api_key)
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self._client = None

    def _get_api_key(self) -> str:
        """Get OpenAI API key."""
        key = self.api_key or config_manager.get_api_key("openai")
        if not key and not self._is_local_server():
            raise AuthenticationError("OpenAI API key not configured")
        return key or "not-needed-for-local"

    def _is_local_server(self) -> bool:
        """Check if using local server (no API key required)."""
        return "localhost" in self.base_url or "127.0.0.1" in self.base_url

    def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._get_api_key(),
                base_url=self.base_url,
                max_retries=0,  # We handle retries in BaseAnalyzer
            )
        return self._client

    def _encode_video_to_base64(self, video_path: str) -> str:
        """Encode video file to base64 string."""
        if not os.path.exists(video_path):
            raise VideoProcessingError(f"Video file not found: {video_path}")

        file_size = os.path.getsize(video_path)
        max_size = (
            100 * 1024 * 1024
        )  # 100MB for local servers (more generous than OpenRouter)

        if file_size > max_size:
            raise VideoProcessingError(
                f"Video file too large ({file_size / 1024 / 1024:.1f}MB). "
                f"Maximum size is {max_size / 1024 / 1024:.0f}MB."
            )

        with open(video_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _get_mime_type(self, video_path: str) -> str:
        """Get MIME type based on file extension."""
        ext = os.path.splitext(video_path)[1].lower()
        mime_types = {
            ".mp4": "video/mp4",
            ".avi": "video/x-msvideo",
            ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
            ".webm": "video/webm",
            ".m4v": "video/x-m4v",
        }
        return mime_types.get(ext, "video/mp4")

    def _extract_video_frames(self, video_path: str) -> List[str]:
        """Extract frames from video for OpenAI vision API."""
        import tempfile
        import subprocess

        frames = []
        try:
            # Create temporary directory for frames
            with tempfile.TemporaryDirectory() as temp_dir:
                # Extract 3-5 frames evenly distributed throughout the video
                # Use ffmpeg to extract frames at specific timestamps
                frame_pattern = os.path.join(temp_dir, "frame_%03d.jpg")

                # Get video duration first
                print(f"[OpenAI] Getting video duration for {video_path}")
                duration_cmd = [
                    "ffprobe",
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    video_path,
                ]
                result = subprocess.run(duration_cmd, capture_output=True, text=True)

                if result.returncode != 0:
                    print(f"[OpenAI] Failed to get video duration: {result.stderr}")
                    raise AnalyzerError(
                        f"Failed to get video duration: {result.stderr}"
                    )

                import json

                info = json.loads(result.stdout)
                duration = float(info["format"]["duration"])
                print(f"[OpenAI] Video duration: {duration} seconds")

                # Extract frames at 25%, 50%, 75% of video duration
                timestamps = [duration * 0.25, duration * 0.5, duration * 0.75]
                print(f"[OpenAI] Will extract frames at timestamps: {timestamps}")

                for i, timestamp in enumerate(timestamps):
                    frame_file = os.path.join(temp_dir, f"frame_{i:03d}.jpg")
                    print(
                        f"[OpenAI] Extracting frame {i + 1} at {timestamp}s to {frame_file}"
                    )
                    extract_cmd = [
                        "ffmpeg",
                        "-ss",
                        str(timestamp),
                        "-i",
                        video_path,
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        frame_file,
                        "-y",
                    ]

                    result = subprocess.run(extract_cmd, capture_output=True)
                    if result.returncode == 0 and os.path.exists(frame_file):
                        print(f"[OpenAI] Successfully extracted frame {i + 1}")
                        # Convert to base64
                        with open(frame_file, "rb") as f:
                            frame_data = base64.b64encode(f.read()).decode("utf-8")
                            frames.append(frame_data)
                            print(
                                f"[OpenAI] Frame {i + 1} encoded to base64 ({len(frame_data)} chars)"
                            )
                    else:
                        print(
                            f"[OpenAI] Failed to extract frame {i + 1}: {result.stderr.decode()}"
                        )

                # If we couldn't extract frames, fall back to single frame at 50%
                if not frames:
                    frame_file = os.path.join(temp_dir, "frame_000.jpg")
                    extract_cmd = [
                        "ffmpeg",
                        "-ss",
                        str(duration * 0.5),
                        "-i",
                        video_path,
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        frame_file,
                        "-y",
                    ]

                    result = subprocess.run(extract_cmd, capture_output=True)
                    if result.returncode == 0 and os.path.exists(frame_file):
                        with open(frame_file, "rb") as f:
                            frame_data = base64.b64encode(f.read()).decode("utf-8")
                            frames.append(frame_data)

        except Exception as e:
            # If frame extraction fails, try to send video as single image (might not work)
            print(
                f"Warning: Frame extraction failed: {e}, falling back to video encoding"
            )
            try:
                video_base64 = self._encode_video_to_base64(video_path)
                frames.append(video_base64)  # This might not work with OpenAI
            except:
                pass

        return frames

    async def analyze_video(
        self, video_path: str, prompt: str, chunk_info: Optional[Dict] = None
    ) -> str:
        """Analyze video using OpenAI API."""
        print(f"[OpenAI] Analyzing video: {video_path}")
        print(f"[OpenAI] Using base URL: {self.base_url}")
        print(f"[OpenAI] Using model: {self.model_name}")
        try:
            client = self._get_client()

            # For OpenAI, extract frames from video since vision API doesn't support video directly
            print(f"[OpenAI] Extracting frames from video...")
            frame_images = self._extract_video_frames(video_path)
            print(f"[OpenAI] Extracted {len(frame_images)} frames")

            # Format prompt with chunk info if provided
            formatted_prompt = prompt
            if chunk_info:
                try:
                    formatted_prompt = prompt.format(
                        chunk_number=chunk_info.get("chunk_number", 1),
                        total_chunks=chunk_info.get("total_chunks", 1),
                        start_time_minutes=round(
                            chunk_info.get("start_time", 0) / 60, 1
                        ),
                        end_time_minutes=round(chunk_info.get("end_time", 0) / 60, 1),
                        duration_minutes=round(chunk_info.get("duration", 0) / 60, 1),
                    )
                except KeyError:
                    pass

            # Build content with extracted frames
            content = []
            for i, frame_base64 in enumerate(frame_images):
                print(f"[OpenAI] Adding frame {i + 1} to request")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{frame_base64}",
                            "detail": "high",
                        },
                    }
                )
            content.append({"type": "text", "text": formatted_prompt})

            print(
                f"[OpenAI] Sending request with {len(frame_images)} frames and prompt length {len(formatted_prompt)}"
            )
            response = await client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": content}],
                max_tokens=4000,  # Generous token limit for video analysis
                temperature=0.1,  # Low temperature for consistent analysis
            )

            print(f"[OpenAI] Received response successfully")
            return response.choices[0].message.content

        except OpenAIError as e:
            error_str = str(e).lower()
            if "rate" in error_str or "429" in error_str:
                raise RateLimitError(f"Rate limit exceeded: {e}")
            elif "auth" in error_str or "401" in error_str or "403" in error_str:
                raise AuthenticationError(f"Authentication failed: {e}")
            else:
                raise AnalyzerError(f"OpenAI API error: {e}")
        except Exception as e:
            raise AnalyzerError(f"OpenAI analysis failed: {e}")

    async def get_available_models(self) -> List[Dict[str, Any]]:
        """Get list of available models from OpenAI API."""
        try:
            client = self._get_client()

            models_response = await client.models.list()
            models = []

            for model in models_response.data:
                model_id = model.id
                model_info = {
                    "id": model_id,
                    "name": model_id,
                    "description": getattr(model, "description", ""),
                    "supports_video": True,  # Assume vision models for video analysis
                }
                models.append(model_info)

            return models if models else self.DEFAULT_MODELS

        except Exception as e:
            # Re-raise exception to let main.py handle it and report error
            raise AnalyzerError(f"Failed to fetch models: {e}")

    def validate_api_key(self) -> bool:
        """Validate OpenAI API key synchronously (deprecated, use async version)."""
        try:
            api_key = self._get_api_key()
            if self._is_local_server():
                return True  # Local servers don't need API keys
            return api_key.startswith("sk-") and len(api_key) > 20
        except Exception:
            return False

    async def validate_api_key_async(self) -> bool:
        """Validate OpenAI API key by making a test request."""
        try:
            client = self._get_client()

            await client.models.list()
            return True

        except OpenAIError as e:
            if "auth" in str(e).lower() or "401" in str(e) or "403" in str(e):
                return False
            # For other errors (rate limits, etc.), consider key valid
            return True
        except Exception:
            # If we can't connect, but it's a local server, consider valid
            if self._is_local_server():
                return True
            return False

    async def combine_analyses(self, analyses: List[str], combine_prompt: str) -> str:
        """Combine multiple chunk analyses into one."""
        try:
            client = self._get_client()

            combined_text = "\n\n---\n\n".join(
                [
                    f"=== Chunk {i + 1} Analysis ===\n{analysis}"
                    for i, analysis in enumerate(analyses)
                ]
            )

            full_prompt = f"{combine_prompt}\n\n{combined_text}"

            response = await client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": full_prompt}],
                max_tokens=4000,
                temperature=0.1,
            )

            return response.choices[0].message.content

        except OpenAIError as e:
            if "rate" in str(e).lower():
                raise RateLimitError(f"Rate limit exceeded: {e}")
            raise AnalyzerError(f"Failed to combine analyses: {e}")
        except Exception as e:
            raise AnalyzerError(f"Failed to combine analyses: {e}")

    async def close(self):
        """Close client connection."""
        if self._client:
            await self._client.close()
            self._client = None
