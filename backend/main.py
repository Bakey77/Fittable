from fastapi import FastAPI                     # FastAPI 实例
from pydantic import BaseModel              
import uvicorn
from schemas.chat import ChatRequest

app = FastAPI()                                  

@app.get("/health")                              
def health():                                     
    return {"status": "ok"}                       

if __name__ == "__main__":                        
    uvicorn.run(app, host="0.0.0.0", port=8000)  

@app.post("/chat")                              # 新增 POST 接口，路径 /chat
def chat(req: ChatRequest):                     # req 的类型是上面定义的 ChatRequest
    return {"response": f"你说了: {req.message}"} 