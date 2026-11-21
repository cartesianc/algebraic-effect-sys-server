from datetime import datetime, timezone
from fastapi import APIRouter
from ...config import get_app_config

router = APIRouter(
  tags=["测试"],
  prefix=f"{get_app_config().api_prefix}/test",
  responses={404: {"description": "Not found"}}
)

@router.get("/")
async def say_hi():
  return {
    "text": "hi",
    'time': datetime.now(timezone.utc).isoformat()
  }

