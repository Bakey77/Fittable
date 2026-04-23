import os                                    # os.getenv 读取系统环境变量
from dotenv import load_dotenv
load_dotenv()                               # 加载 .env 文件，让 os.getenv 能读到里面的变量

class Config:
    # 模型配置（必需的三件套）
    LLM_BASE_URL = os.getenv("LLM_BASE_URL")  # 从环境变量读取 API 地址，没有返回 None
    LLM_API_KEY = os.getenv("LLM_API_KEY")    # 从环境变量读取 API 密钥
    LLM_MODEL = os.getenv("LLM_MODEL")        # 从环境变量读取模型名称

    # VLM：能看图片的多模态模型，用于食物图片分析
    # EMBEDDING：文本向量化模型，用于知识库检索
    VLM_MODEL = os.getenv("VLM_MODEL")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")