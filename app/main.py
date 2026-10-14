"""
FastAPI main application for Video Analyzer Web.
"""

import os
import logging
import hashlib
from pathlib import Path
import uuid
from datetime import datetime
from typing import Dict, Optional
from fastapi import (
    FastAPI,
    File,
    UploadFile,
    HTTPException,
    Request,
    Form,
    BackgroundTasks,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

from app.config import config_manager
from app.prompts import prompt_manager, VIDEO_TYPES
from app.video_processor import VideoProcessor
from app.analyzers import (
    GeminiAnalyzer,
    OpenRouterAnalyzer,
    OpenAIAnalyzer,
    AnalyzerError,
    AuthenticationError,
)


# Initialize FastAPI app
app = FastAPI(
    title="Video Analyzer",
    description="AI-powered video analysis with Gemini and OpenRouter",
    version="1.0.0",
)

# Setup static files and templates
BASE_DIR = Path(__file__).resolve().parent.parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Ensure directories exist
for dir_name in ["uploads", "outputs", "temporary"]:
    os.makedirs(BASE_DIR / dir_name, exist_ok=True)


# ============== Models ==============


class APIKeyRequest(BaseModel):
    provider: str
    api_key: str


class ResetKeyRequest(BaseModel):
    provider: str


class AnalysisRequest(BaseModel):
    video_type: str
    provider: str
    model: str
    custom_prompt: Optional[str] = None
    with_keyframes: bool = False


class KeyframeItem(BaseModel):
    timecode: str
    title: str
    frame_description: Optional[str] = None


class ExtractKeyframesRequest(BaseModel):
    filename: str
    keyframes: list[KeyframeItem]


class JobStatus(BaseModel):
    job_id: str
    status: str  # pending, processing, completed, failed
    progress: int
    current_step: str
    result: Optional[str] = None
    error: Optional[str] = None


# ============== Job Storage ==============

# In-memory job storage (in production, use Redis or database)
jobs: Dict[str, Dict] = {}


# ============== API Endpoints ==============


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render main page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/config")
async def get_config():
    """Get current configuration status."""
    settings = config_manager.settings
    return {
        "gemini_configured": config_manager.has_valid_api_key("gemini"),
        "openrouter_configured": config_manager.has_valid_api_key("openrouter"),
        "openai_configured": config_manager.has_valid_api_key("openai"),
        "default_provider": settings.analyzer_type,
        "default_gemini_model": settings.gemini_model_name,
        "default_openrouter_model": settings.openrouter_model_name,
        "default_openai_model": settings.openai_model_name,
        "max_upload_size_mb": settings.max_upload_size_mb,
        "chunk_duration_minutes": settings.chunk_duration_minutes,
    }


@app.get("/api/video-types")
async def get_video_types():
    """Get available video analysis types."""
    return prompt_manager.get_available_types()


@app.get("/api/models/{provider}")
async def get_models(provider: str):
    """Get available models for a provider."""
    if provider not in ["gemini", "openrouter", "openai"]:
        raise HTTPException(status_code=400, detail="Invalid provider")

    try:
        if provider == "gemini":
            api_key = config_manager.get_api_key("gemini")
            # If no API key, return empty list (client will show API key input)
            if not api_key:
                return {"models": GeminiAnalyzer.DEFAULT_MODELS, "from_api": False}

            analyzer = GeminiAnalyzer(api_key=api_key)
            models = await analyzer.get_available_models()
            return {"models": models, "from_api": True}

        elif provider == "openrouter":
            api_key = config_manager.get_api_key("openrouter")
            if not api_key:
                return {"models": OpenRouterAnalyzer.DEFAULT_MODELS, "from_api": False}

            analyzer = OpenRouterAnalyzer(api_key=api_key)
            models = await analyzer.get_available_models()
            return {"models": models, "from_api": True}

        elif provider == "openai":
            api_key = config_manager.get_api_key("openai")
            base_url = config_manager.get_openai_base_url()
            # We need a model name to initialize, even if we're just listing models
            default_model = config_manager.settings.openai_model_name
            analyzer = OpenAIAnalyzer(
                model_name=default_model, api_key=api_key, base_url=base_url
            )
            models = await analyzer.get_available_models()
            return {"models": models, "from_api": True}

    except Exception as e:
        # Return default models on error with error message
        if provider == "gemini":
            return {
                "models": GeminiAnalyzer.DEFAULT_MODELS,
                "from_api": False,
                "error": str(e),
            }
        elif provider == "openrouter":
            return {
                "models": OpenRouterAnalyzer.DEFAULT_MODELS,
                "from_api": False,
                "error": str(e),
            }
        elif provider == "openai":
            # No invented fallback list: an empty select plus a real error
            # beats a dropdown of model names the server never reported.
            return {
                "models": [],
                "from_api": False,
                "error": str(e),
            }


@app.get("/api/prompt/{video_type}")
async def get_prompt(video_type: str):
    """Get prompt template for video type."""
    try:
        prompt = prompt_manager.load_prompt(video_type)
        type_info = VIDEO_TYPES.get(video_type, {})
        return {"prompt": prompt, "type_info": type_info}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/keyframes-criteria-default")
async def get_keyframes_criteria_default():
    """Get default keyframes criteria."""
    try:
        criteria = prompt_manager.get_keyframes_criteria_default()
        return {"criteria": criteria}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/set-api-key")
async def set_api_key(request: APIKeyRequest):
    """Set API key for a provider at runtime."""
    if request.provider not in ["gemini", "openrouter", "openai"]:
        raise HTTPException(status_code=400, detail="Invalid provider")

    config_manager.set_runtime_api_key(request.provider, request.api_key)

    try:
        if request.provider == "gemini":
            analyzer = GeminiAnalyzer(api_key=request.api_key)
        elif request.provider == "openrouter":
            analyzer = OpenRouterAnalyzer(api_key=request.api_key)
        else:  # openai
            analyzer = OpenAIAnalyzer(
                model_name=config_manager.settings.openai_model_name,
                api_key=request.api_key,
                base_url=config_manager.get_openai_base_url(),
            )

        try:
            models = await analyzer.get_available_models()
        finally:
            await analyzer.close()

        return {"success": True, "models": models, "from_api": True}

    except Exception as e:
        # The key did not work — do not leave a broken one behind.
        config_manager.set_runtime_api_key(request.provider, "")
        if request.provider == "gemini":
            return {
                "success": False,
                "models": GeminiAnalyzer.DEFAULT_MODELS,
                "from_api": False,
                "error": str(e),
            }
        elif request.provider == "openrouter":
            return {
                "success": False,
                "models": OpenRouterAnalyzer.DEFAULT_MODELS,
                "from_api": False,
                "error": str(e),
            }
        else:
            return {"success": False, "models": [], "from_api": False, "error": str(e)}


@app.post("/api/reset-api-key")
async def reset_api_key(request: ResetKeyRequest):
    """Forget the runtime API key for a provider (env values are untouched)."""
    if request.provider not in ["gemini", "openrouter", "openai"]:
        raise HTTPException(status_code=400, detail="Invalid provider")

    config_manager.set_runtime_api_key(request.provider, "")
    return {"success": True, "message": f"{request.provider} API key has been reset"}


@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    """Upload a video file."""
    # Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    allowed_extensions = {
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".webm",
        ".m4v",
        ".wmv",
        ".flv",
    }
    ext = Path(file.filename).suffix.lower()

    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"File type not supported. Allowed: {', '.join(allowed_extensions)}",
        )

    # Read file content first
    content = await file.read()
    max_size = config_manager.settings.max_upload_size_mb * 1024 * 1024

    if len(content) > max_size:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size: {config_manager.settings.max_upload_size_mb}MB",
        )

    # Calculate file hash for deduplication
    file_hash = hashlib.sha256(content).hexdigest()[:16]  # 16 char hash
    logger.info(f"Calculated hash {file_hash} for file {file.filename}")

    # Check if file with this hash already exists
    uploads_dir = BASE_DIR / "uploads"
    uploads_dir.mkdir(exist_ok=True)

    existing_files = list(uploads_dir.glob(f"{file_hash}*"))
    if existing_files:
        # File already exists, return info about existing file
        existing_file = existing_files[0]
        logger.info(f"File with hash {file_hash} already exists: {existing_file.name}")
        processor = VideoProcessor()
        video_info = processor.get_video_info(str(existing_file))

        return {
            "file_id": file_hash,
            "filename": existing_file.name,
            "original_name": file.filename,
            "size_bytes": len(content),
            "video_info": video_info,
            "cached": True,
        }

    # Generate hash-based filename
    safe_filename = f"{file_hash}_{Path(file.filename).stem}{ext}"
    file_path = uploads_dir / safe_filename

    # Save file
    try:
        with open(file_path, "wb") as f:
            f.write(content)

        # Get video info
        processor = VideoProcessor()
        video_info = processor.get_video_info(str(file_path))

        return {
            "file_id": file_hash,
            "filename": safe_filename,
            "original_name": file.filename,
            "size_bytes": len(content),
            "video_info": video_info,
            "cached": False,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload file: {e}")


@app.post("/api/analyze")
async def start_analysis(
    background_tasks: BackgroundTasks,
    file_id: str = Form(...),
    filename: str = Form(...),
    video_type: str = Form(...),
    provider: str = Form(...),
    model: str = Form(...),
    custom_prompt: Optional[str] = Form(None),
    with_keyframes: bool = Form(False),
    custom_keyframes_criteria: Optional[str] = Form(None),
):
    """Start video analysis job."""
    # Validate inputs
    file_path = BASE_DIR / "uploads" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    # OpenAI supports local APIs without API key
    if provider != "openai" and not config_manager.has_valid_api_key(provider):
        raise HTTPException(
            status_code=400,
            detail=f"API key for {provider} not configured. Please set it first.",
        )

    # Create job
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "pending",
        "progress": 0,
        "current_step": "Initializing...",
        "result": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
        "file_path": str(file_path),
        "video_type": video_type,
        "provider": provider,
        "model": model,
    }

    # Start background processing
    background_tasks.add_task(
        process_video_job,
        job_id,
        str(file_path),
        video_type,
        provider,
        model,
        custom_prompt,
        with_keyframes,
        custom_keyframes_criteria,
    )

    return {"job_id": job_id, "status": "pending"}


@app.get("/api/job/{job_id}")
async def get_job_status(job_id: str):
    """Get status of an analysis job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    return JobStatus(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        current_step=job["current_step"],
        result=job.get("result"),
        error=job.get("error"),
    )


@app.post("/api/extract-keyframes")
async def extract_keyframes(request: ExtractKeyframesRequest):
    """Extract keyframes from video and return ZIP archive."""
    # Validate video file exists
    file_path = BASE_DIR / "uploads" / request.filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    if not request.keyframes:
        raise HTTPException(status_code=400, detail="No keyframes provided")

    # Create unique job id for this extraction
    extraction_id = str(uuid.uuid4())[:8]

    # Prepare output path
    video_name = Path(request.filename).stem
    output_dir = BASE_DIR / "outputs" / video_name
    os.makedirs(output_dir, exist_ok=True)

    zip_filename = f"{video_name}_keyframes_{extraction_id}.zip"
    zip_path = output_dir / zip_filename

    try:
        processor = VideoProcessor(temp_dir=str(BASE_DIR / "temporary"))

        # Convert pydantic models to dicts
        keyframes_list = [kf.model_dump() for kf in request.keyframes]

        result = processor.extract_keyframes_to_zip(
            video_path=str(file_path),
            keyframes=keyframes_list,
            output_zip_path=str(zip_path),
            job_id=extraction_id,
        )

        if not result["success"] or result["extracted_count"] == 0:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to extract keyframes. Extracted: {result['extracted_count']}, Failed: {result['failed_count']}",
            )

        return FileResponse(
            path=str(zip_path), filename=zip_filename, media_type="application/zip"
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to extract keyframes: {str(e)}"
        )


# ============== Background Processing ==============


async def process_video_job(
    job_id: str,
    file_path: str,
    video_type: str,
    provider: str,
    model: str,
    custom_prompt: Optional[str],
    with_keyframes: bool,
    custom_keyframes_criteria: Optional[str] = None,
):
    """Process video analysis in background."""
    logger.info(
        f"Starting job {job_id} for file {file_path} with provider {provider}, model {model}"
    )
    analyzer = None
    processor = None
    try:
        jobs[job_id]["status"] = "processing"
        jobs[job_id]["current_step"] = "Loading prompt template..."
        jobs[job_id]["progress"] = 5

        # Load prompt
        if custom_prompt and custom_prompt.strip():
            prompt = custom_prompt
            logger.info(f"Using custom prompt for job {job_id}")
        else:
            # Pass custom keyframes criteria if provided
            prompt = prompt_manager.load_prompt(
                video_type,
                with_keyframes,
                custom_keyframes_criteria
                if custom_keyframes_criteria and custom_keyframes_criteria.strip()
                else None,
            )
            logger.info(
                f"Loaded prompt template for video type {video_type}, keyframes: {with_keyframes}"
            )

        # Always append fixed keyframes JSON format if keyframes are enabled
        if with_keyframes:
            keyframes_format = prompt_manager.get_keyframes_format()
            if keyframes_format:
                prompt += "\n\n" + keyframes_format

        jobs[job_id]["current_step"] = "Analyzing video duration..."
        jobs[job_id]["progress"] = 10

        # Initialize processor and analyzer
        processor = VideoProcessor(temp_dir=str(BASE_DIR / "temporary"))

        api_key = config_manager.get_api_key(provider)
        logger.info(f"Initializing {provider} analyzer with model {model}")
        if provider == "gemini":
            analyzer = GeminiAnalyzer(model_name=model, api_key=api_key)
        elif provider == "openrouter":
            analyzer = OpenRouterAnalyzer(model_name=model, api_key=api_key)
        else:  # openai
            base_url = config_manager.get_openai_base_url()
            analyzer = OpenAIAnalyzer(
                model_name=model, api_key=api_key, base_url=base_url
            )

        # Prepare output directories early to save intermediate results
        video_name = Path(file_path).stem
        output_dir = BASE_DIR / "outputs" / video_name
        os.makedirs(output_dir, exist_ok=True)
        chunks_dir = output_dir / "chunks"
        os.makedirs(chunks_dir, exist_ok=True)

        # Split video into chunks
        jobs[job_id]["current_step"] = "Splitting video into chunks..."
        jobs[job_id]["progress"] = 15

        chunks = processor.split_video(file_path, job_id)
        total_chunks = len(chunks)

        jobs[job_id]["current_step"] = f"Processing {total_chunks} chunk(s)..."
        jobs[job_id]["progress"] = 20

        # Analyze each chunk
        analyses = []
        logger.info(f"Starting analysis of {total_chunks} chunks for job {job_id}")
        for i, chunk in enumerate(chunks):
            chunk_progress = 20 + int((i / total_chunks) * 60)
            jobs[job_id]["progress"] = chunk_progress
            jobs[job_id]["current_step"] = f"Analyzing chunk {i + 1}/{total_chunks}..."

            logger.info(
                f"Processing chunk {i + 1}/{total_chunks} for job {job_id}: {chunk['path']}"
            )
            try:
                analysis = await analyzer.analyze_with_retry(
                    chunk["path"], prompt, chunk
                )
                analyses.append(analysis)

                # Save immediate chunk result
                chunk_file = chunks_dir / f"chunk_{i + 1:03d}.md"
                with open(chunk_file, "w", encoding="utf-8") as f:
                    f.write(f"# Analysis for Chunk {i + 1}\n\n{analysis}")

                logger.info(f"Successfully analyzed chunk {i + 1} for job {job_id}")
            except Exception as e:
                error_msg = f"Failed on chunk {i + 1}: {str(e)}"
                logger.error(f"Job {job_id} failed: {error_msg}")
                jobs[job_id]["error"] = error_msg
                jobs[job_id]["status"] = "failed"
                return

        # Combine analyses if multiple chunks
        jobs[job_id]["progress"] = 85
        jobs[job_id]["current_step"] = "Combining analyses..."

        final_analysis = ""
        if len(analyses) > 1:
            try:
                combine_prompt = prompt_manager.load_combine_prompt()
                final_analysis = await analyzer.combine_analyses(
                    analyses, combine_prompt
                )
            except Exception as e:
                logger.warning(f"Failed to combine analyses: {e}")
                # Fallback: Concatenate chunks so work is not lost
                final_analysis = "# Comprehensive Analysis (Fallback)\n\n"
                final_analysis += "> **Warning:** The AI model could not summarize the full video due to length limits or an error. Below are the individual chunk analyses.\n\n"
                final_analysis += f"**Error details:** {str(e)}\n\n"
                final_analysis += "\n\n---\n\n".join(
                    [
                        f"## Part {i + 1}\n{analysis}"
                        for i, analysis in enumerate(analyses)
                    ]
                )
        else:
            final_analysis = analyses[0]

        # Save result
        jobs[job_id]["progress"] = 95
        jobs[job_id]["current_step"] = "Saving results..."

        output_file = output_dir / f"{video_name}_analysis.md"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"# Video Analysis: {video_name}\n\n")
            f.write(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"**Provider:** {provider}\n")
            f.write(f"**Model:** {model}\n")
            f.write(
                f"**Video Type:** {VIDEO_TYPES.get(video_type, {}).get('name', video_type)}\n\n"
            )
            f.write("---\n\n")
            f.write(final_analysis)

        # Complete
        jobs[job_id]["progress"] = 100
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["current_step"] = "Analysis complete!"
        jobs[job_id]["result"] = final_analysis

    except AuthenticationError as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = f"Authentication error: {str(e)}"
    except AnalyzerError as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = f"Analysis error: {str(e)}"
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = f"Unexpected error: {str(e)}"
    finally:
        # Analyzer clients and temp chunks are per job — do not leak either,
        # including on the failure paths.
        if analyzer is not None:
            await analyzer.close()
        if processor is not None:
            processor.cleanup_job(job_id)


# ============== Startup/Shutdown ==============


@app.on_event("startup")
async def startup():
    """Initialize on startup."""
    # Ensure prompt files exist
    prompts_dir = BASE_DIR / "prompts"
    if not prompts_dir.exists() or not any(prompts_dir.iterdir()):
        print("Warning: Prompts directory is empty. Please copy prompt files.")


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown."""
    # Clean up any remaining temp files
    import shutil

    temp_dir = BASE_DIR / "temporary"
    if temp_dir.exists():
        for item in temp_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
