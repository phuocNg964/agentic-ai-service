from pydantic import BaseModel
from typing import List, Optional, Any

class ChatRequest(BaseModel):
    query: str
    thread_id: str = "default_thread"

class ChatResponse(BaseModel):
    response: str
    thread_id: str
    metadata: Optional[dict] = None
    
class HealthCheck(BaseModel):
    status: str = "ok"
