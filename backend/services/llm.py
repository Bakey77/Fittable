from langchain_openai import ChatOpenAI
try:
    from config import Config
except ImportError:
    from backend.services.config import Config

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
