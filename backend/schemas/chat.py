from pydantic import BaseModel


class ChatRequest(BaseModel):                    # 定义请求体格式，FastAPI 会自动校验
    message: str 