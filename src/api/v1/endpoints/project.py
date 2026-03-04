from fastapi import APIRouter, HTTPException, Header
from typing import Optional
from src.schemas.chat import ChatRequest, ChatResponse
# Import the agent class
from src.agents.project_manager.agent import AgenticProjectManager
from src.core.context import set_request_token
from langchain_core.messages import HumanMessage

router = APIRouter()

# Global instance (In production, use dependency injection or cache)
agent_system = AgenticProjectManager()
# Note: Re-building graph might be needed if tools depend on init, but they are dynamic.

@router.post("/chat", response_model=ChatResponse)
def chat_project_manager(
    request: ChatRequest,
    authorization: Optional[str] = Header(None)
):
    """
    Send a message to the Project Manager AI Agent.
    """
    try:
        # Set Auth Context
        if authorization:
            token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
            set_request_token(token)

        # Prepare input for LangGraph
        initial_state = {
            "messages": [HumanMessage(content=request.query)],
            "query": request.query
        }
        
        # Invoke the graph
        # Pass thread_id to enable memory checkpointing
        config = {"configurable": {"thread_id": request.thread_id}}
        result = agent_system.graph.invoke(initial_state, config=config)
        
        # Extract the last message content
        last_message = result['messages'][-1]
        
        # Handle structured content (list of blocks) vs string content
        if isinstance(last_message.content, list):
            # Extract text from blocks where type is 'text'
            text_parts = []
            for block in last_message.content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    text_parts.append(block.get('text', ''))
                elif isinstance(block, str):
                    text_parts.append(block)
            response_text = "\n".join(text_parts)
        else:
            response_text = str(last_message.content)
        
        return ChatResponse(
            response=response_text,
            thread_id=request.thread_id
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
