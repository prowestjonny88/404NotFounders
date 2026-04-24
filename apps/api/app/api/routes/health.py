from fastapi import APIRouter
from app.core.config import settings
from app.providers.llm_provider import GLMProvider

router = APIRouter()

@router.get("/health")
def health_check():
    return {
        "status": "ok", 
        "model": settings.MODEL_NAME
    }


@router.get("/health/langfuse")
def langfuse_health_check():
    return GLMProvider.langfuse_status()
