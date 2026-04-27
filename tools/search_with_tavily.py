from langchain_tavily import TavilySearch
from config import Config

def search_with_tavily(query: str) -> str:
    """使用Tavily进行网络搜索"""
    tavily = TavilySearch(api_key=Config.TAVILY_API_KEY)
    result = tavily.invoke(query)

    if result:
        return result if isinstance(result, str) else str(result)
    return ""
