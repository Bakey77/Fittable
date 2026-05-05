import os                                    # os.getenv 读取系统环境变量
from dotenv import load_dotenv
load_dotenv()                               # 加载 .env 文件，让 os.getenv 能读到里面的变量

class Config:
    # 模型配置（必需的三件套）
    LLM_BASE_URL = os.getenv("LLM_BASE_URL")  # 从环境变量读取 API 地址，没有返回 None
    LLM_API_KEY = os.getenv("LLM_API_KEY")    # 从环境变量读取 API 密钥
    LLM_MODEL = os.getenv("LLM_MODEL")        # 从环境变量读取模型名称

    # Longcat（Chat LLM）
    LONGCAT_BASE_URL = os.getenv("LONGCAT_BASE_URL")
    LONGCAT_API_KEY = os.getenv("LONGCAT_API_KEY")
    LONGCAT_MODEL = os.getenv("LONGCAT_MODEL")

    # VLM：能看图片的多模态模型，用于食物图片分析
    # EMBEDDING：文本向量化模型，用于知识库检索
    VLM_MODEL = os.getenv("VLM_MODEL")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
    RERANK_MODEL = os.getenv("RERANK_MODEL")
    
    QDRANT_HOST = os.getenv("QDRANT_HOST")
    QDRANT_PORT = os.getenv("QDRANT_PORT")

    REDIS_HOST = os.getenv("REDIS_HOST","localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
    REDIS_DB = int(os.getenv("REDIS_DB", 0))
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
    REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", 10))
    REDIS_SOCKET_TIMEOUT = int(os.getenv("REDIS_SOCKET_TIMEOUT", 5))

    THREAD_POOL_MAX_WORKERS= int(os.getenv("THREAD_POOL_MAX_WORKERS", 10))

