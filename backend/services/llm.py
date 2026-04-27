import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

from langchain_openai import ChatOpenAI
from config import Config

try:
    from llama_index.embeddings.dashscope import (
        DashScopeBatchTextEmbeddingModels,
        DashScopeEmbedding,
        DashScopeTextEmbeddingModels,
    )
except ImportError:
    DashScopeEmbedding = None
    DashScopeBatchTextEmbeddingModels = None
    DashScopeTextEmbeddingModels = None

_embedding_model_instance = None

def get_llm():
    return ChatOpenAI(
        api_key=Config.LLM_API_KEY,
        base_url=Config.LLM_BASE_URL,
        model=Config.LLM_MODEL,
    )  
# respond = get_llm().invoke(
#     [{"role": "user", "content": "你好"}] 
# )
# print(respond.content)
def get_embedding_model():
    global _embedding_model_instance
    if DashScopeEmbedding is None:
        raise ImportError(
            "DashScope embedding plugin is missing. "
            "Install with: pip install llama-index-embeddings-dashscope"
        )

    if _embedding_model_instance is None:
        _embedding_model_instance = DashScopeEmbedding(
            api_key=Config.LLM_API_KEY,
            base_url=Config.LLM_BASE_URL,
            model=Config.EMBEDDING_MODEL,
        )
    return _embedding_model_instance
