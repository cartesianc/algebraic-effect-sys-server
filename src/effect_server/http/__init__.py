import importlib
import pkgutil
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import router
from .exception import HttpServiceException
from .log import logger

@asynccontextmanager
async def lifespan(app: FastAPI):
  logger.info("应用程序启动")
  try:
    yield
  finally:
    logger.info("应用程序关闭")


def create_app() -> FastAPI:
  """创建并配置 FastAPI 应用实例"""
  app = FastAPI(lifespan=lifespan)
  # 安装路由
  install_router(app)
  
  # 添加中间件
  app.add_middleware(
      CORSMiddleware,
      allow_origins=["*"],
      allow_credentials=True,
      allow_methods=["*"],
      allow_headers=["*"],
  )
  
  
  @app.exception_handler(HttpServiceException)
  async def service_exception_handler(request: Request, exc: HttpServiceException):
    return JSONResponse(status_code=exc.code, content=exc.to_json())

  return app

def install_router(app: FastAPI):
  """装载路由"""
  for module_info in pkgutil.walk_packages(router.__path__, router.__name__ + '.'):
    module_name = module_info.name
      
    is_pkg = module_info.ispkg
    try:
      loaded_module = importlib.import_module(module_name)
      logger.debug(f"已加载模块: {module_name} (包: {'是' if is_pkg else '否'})")
      _router = getattr(loaded_module, 'router', None)
      if _router != None:
          app.include_router(_router)
              
    except ImportError as e:
      logger.error(f"加载模块 {module_name} 时发生错误: {e}")
      sys.exit(5)

# 创建应用实例
app = create_app()
