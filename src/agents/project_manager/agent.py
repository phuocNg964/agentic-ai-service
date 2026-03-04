
import json
import logging
from typing import TypedDict, Annotated, Literal, List, Optional
import operator
from pydantic import BaseModel, Field


from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage, AnyMessage

# Relative imports
from ...models.models import call_llm

from .api_tools import ALL_API_TOOLS
import os
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool



logger = logging.getLogger(__name__)

# Safety limit for tool call iterations
MAX_TOOL_ITERATIONS = 10

# --- CONFIGURATION ---
param_dict = {
    'router_kwargs': {
        'model_provider': 'gemini',
        'model_name': 'gemini-2.0-flash-lite',
        'temperature': 0.1,
        'top_p': 0.3, 
        'max_tokens': 200,
    },
    'direct_kwargs': {
        'model_provider': 'gemini',
        'model_name': 'gemini-2.5-flash',
        'temperature': 1,
        'top_p': 0.9,
        'max_tokens': 500,
    },
    'large_deterministic_kwargs': { # tool_call
        'model_provider': 'gemini',
        'model_name': 'gemini-2.5-flash',
        'temperature': 0.1,
        'top_p': 0.3,
    },
}

# --- SCHEMAS ---
class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    query: str
    router_decision: str  # "DIRECT" or "TOOL_CALL"
    iteration_count: int  # Track tool call iterations to prevent infinite loops

class RouterOutput(BaseModel):
    """Schema for Router"""
    decision: Literal["TOOL_CALL", "DIRECT"] = Field(
        description="Quyết định phân luồng: 'TOOL_CALL' cho các yêu cầu cần gọi API/công cụ, 'DIRECT' cho hội thoại/trả lời trực tiếp."
    )

# --- AGENT CLASS ---
class AgenticProjectManager:
    def __init__(self, tools: Optional[List] = None):
        """
        Initialize the Agentic system.
        
        Args:
            tools: Optional list of tools to use. If None, uses ALL_API_TOOLS.
        """
        # self.current_user_id = current_user_id # Removed per refactor
        
        self.llm_router = call_llm(**param_dict['router_kwargs'])
        self.llm_direct = call_llm(**param_dict['direct_kwargs'])
        self.llm_tool_call = call_llm(**param_dict['large_deterministic_kwargs'])
        
        # Use provided tools or fallback to ALL_API_TOOLS
        self.tools_list = tools if tools is not None else ALL_API_TOOLS
        self.tools = {t.name: t for t in self.tools_list}
        self.llm_tool_call = self.llm_tool_call.bind_tools(self.tools_list)
        
        # Memory Checkpointer Setup
        self.db_url = os.getenv("DATABASE_URL")
        if not self.db_url:
            logger.warning("DATABASE_URL not found. Memory persistence will effectively be disabled (or fail if graph requires it).")
        
        # Initialize the connection pool (basic setup)
        # Note: In a production app, you might want to share this pool or manage it globally.
        self.pool = ConnectionPool(conninfo=self.db_url, min_size=1, max_size=10, kwargs={"autocommit": True})
        self.checkpointer = PostgresSaver(self.pool)

        # Ensure tables exist (Blocking call on init)
        self.checkpointer.setup()

        self.graph = self.build_graph()
    
    def build_graph(self) -> StateGraph:
        builder = StateGraph(AgentState)
        
        # Intent classifier
        builder.add_node('router', self.router)

        # Tool Call nodes
        builder.add_node('tool_call', self.take_action)
        builder.add_node('tool_generator', self.tool_generator)

        # DIRECT nodes
        builder.add_node('direct_generator', self.direct_generator)
        
        builder.set_entry_point('router')
        # Edges
        builder.add_conditional_edges(
            'router',
            self._intent_classify,
            {
                'TOOL_CALL': 'tool_generator',
                'DIRECT': 'direct_generator'
            }
        )
        
        builder.add_edge('direct_generator', END)
        
        # Tool Call route
        builder.add_conditional_edges(
            'tool_generator',
            self._exist_tool,
            {
                True: 'tool_call',
                False: END
            }
        )
        builder.add_edge('tool_call', 'tool_generator')
        
        return builder.compile(checkpointer=self.checkpointer)
    
    
    # Router node
    def router(self, state: AgentState):
        """Router node to decide between Tool Call and Direct generation"""
        
        query = state['query']
        prompt = """Phân loại câu hỏi vào 1 trong 2 nhánh:

DIRECT - Trả lời trực tiếp:
• Chào hỏi, cảm ơn, small talk
• Viết email, dịch thuật, soạn văn bản
• Kiến thức chung (Agile, Scrum, REST API...)

TOOL_CALL - Truy xuất/Thao tác dữ liệu:
• Thông tin Dự án, Tasks, Meetings
• Tìm kiếm, tra cứu dữ liệu hệ thống
• Thông tin cá nhân (Tasks của tôi, Profile)
• Bất kỳ câu hỏi nào cần tra cứu ngữ cảnh dự án

VÍ DỤ:
• "Viết email xin hoãn deadline" → DIRECT
• "Tasks của tôi" → TOOL_CALL
• "Dự án A có gì?" → TOOL_CALL
• "Danh sách task trong đó" -> TOOL_CALL
"""
        # Inject history to understand context (e.g., "dự án đầu tiên")
        # Sanitize history to text to avoid Gemini 400 errors with tool structures
        raw_history = state.get('messages', [])[-10:]
        history_context = ""
        for msg in raw_history:
            if isinstance(msg, HumanMessage):
                history_context += f"User: {msg.content}\n"
            elif isinstance(msg, AIMessage):
                content = msg.content if msg.content else "[Tool Call Generated]"
                history_context += f"AI: {content}\n"
            
        full_prompt = f"{prompt}\n\nLỊCH SỬ HỘI THOẠI:\n{history_context}"

        messages = [
            SystemMessage(content=full_prompt),
            HumanMessage(content=query)
        ]
        
        response = self.llm_router.with_structured_output(RouterOutput).invoke(messages)
        
        if not response:
            logger.error("Router response is None or empty")
            decision = "DIRECT"
        elif isinstance(response, dict):
            decision = response.get('decision', 'DIRECT')
        else:
            try:
                decision = response.decision
            except AttributeError:
                logger.error(f"Router response object {type(response)} has no attribute 'decision'")
                decision = "DIRECT"

        logger.info(f"Router decision: {decision}")
        
        return {'router_decision': decision}
    
    # Tool nodes
    def tool_generator(self, state: AgentState) -> dict:
        """Generate tool calls if necessary. Uses proper message history for multi-turn."""
        
        query = state['query']
        current_iteration = state.get('iteration_count', 0)
        
        tool_prompt = """Bạn là PM Assistant - Trợ lý quản lý dự án.

NHIỆM VỤ: Sử dụng linh hoạt các Tools để trả lời user. Dữ liệu hệ thống là UUID, nhưng User sẽ hỏi bằng TÊN tự nhiên.

QUY TẮC BẤT BIẾN:
Nếu cần gọi một tool yêu cầu `_id` (project_id, task_id, meeting_id, user_id) mà bạn chưa có UUID đó:
-> BẠN PHẢI TÌM NÓ TRƯỚC. KHÔNG ĐƯỢC HỎI USER.

BẢN ĐỒ DỮ LIỆU (Cách tìm ID):
1. **Project ID**:
   - Cổng vào duy nhất: `get_user_projects()`.
   - Tìm tên dự án user nói -> Lấy ID.

2. **Task ID**:
   - Cần Project ID trước.
   - Gọi `get_project_tasks(project_id)`.
   - Duyệt danh sách -> Tìm task có title khớp -> Lấy Task ID.
   - Dùng ID này cho: `update_task_status`, xem chi tiết task...

3. **Meeting ID**:
   - Cần Project ID trước.
   - Gọi `get_project_meetings(project_id)`.
   - Tìm cuộc họp theo chủ đề/thời gian -> Lấy Meeting ID.

4. **User ID** (Assignee):
   - **Người khác**: Cần Project ID -> Gọi `get_project_details(project_id)` -> Map tên sang ID.
   - **"Tôi" (Current User)**: Gọi `get_current_user_info()` -> Lấy ID của người đang chat.

VÍ DỤ TƯ DUY:
- User: "Dời lịch họp 'Kickoff' sang mai" -> Tìm Project -> Tìm Meeting ID.
- User: "Giao task này cho TÔI" -> Gọi `get_current_user_info()` lấy ID -> Update task.

LƯU Ý CUỐI:
- Luôn kiểm tra kỹ danh sách trả về để tìm khớp tên nhất có thể.
- Nếu không tìm thấy tên khớp, hãy liệt kê các mục khả dĩ để User chọn.
- **TUYỆT ĐỐI KHÔNG hiển thị UUID/ID trong câu trả lời cho User.** Chỉ dùng Tên (Name/Title). Việc để lộ ID bị coi là lỗi nghiêm trọng.
- Khi đã có đủ thông tin từ Tool Output, hãy TRẢ LỜI TRỰC TIẾP bằng text. KHÔNG gọi thêm tool nếu không cần thiết.
"""

        # Build message history with proper separation:
        # 1. CROSS-TURN HISTORY: Only HumanMessage + final AIMessage (text-only context)
        # 2. CURRENT TURN: Full messages including ToolMessage for tool-calling loop
        
        raw_history = state.get('messages', [])
        
        # Find the CURRENT turn's start (last HumanMessage)
        current_turn_start = -1
        for i in range(len(raw_history) - 1, -1, -1):
            if isinstance(raw_history[i], HumanMessage):
                current_turn_start = i
                break
        
        # Separate histories
        if current_turn_start > 0:
            previous_turns = raw_history[:current_turn_start]
            current_turn = raw_history[current_turn_start:]
        else:
            previous_turns = []
            current_turn = raw_history if current_turn_start == 0 else []
        
        # --- CROSS-TURN CONTEXT (text-based, no ToolMessage) ---
        # Extract only HumanMessage + final AIMessage content from previous turns
        cross_turn_context = ""
        for msg in previous_turns[-10:]:  # Limit to last 10 messages from prev turns
            if isinstance(msg, HumanMessage):
                cross_turn_context += f"User: {msg.content}\n"
            elif isinstance(msg, AIMessage):
                # Only include AIMessage with text content (final answers)
                # Skip AIMessage with tool_calls (intermediate steps)
                if msg.content and not (hasattr(msg, 'tool_calls') and msg.tool_calls):
                    cross_turn_context += f"AI: {msg.content}\n"
        
        # --- CURRENT TURN MESSAGES (full, including ToolMessage) ---
        # Sanitize current turn messages for Gemini's strict ordering
        current_turn_messages = []
        for msg in current_turn:
            if isinstance(msg, HumanMessage):
                current_turn_messages.append(msg)
            elif isinstance(msg, AIMessage):
                # Ensure AIMessage has content (Gemini may reject empty content with tool_calls)
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    current_turn_messages.append(AIMessage(
                        content=msg.content if msg.content else "[Processing...]",
                        tool_calls=msg.tool_calls
                    ))
                elif msg.content:
                    current_turn_messages.append(msg)
            elif isinstance(msg, ToolMessage):
                # Include ToolMessage for current turn's tool-calling loop
                current_turn_messages.append(msg)
        
        # Ensure current turn starts with HumanMessage
        if not current_turn_messages or not isinstance(current_turn_messages[0], HumanMessage):
            current_turn_messages.insert(0, HumanMessage(content=query))
        
        # Build system prompt with cross-turn context
        if cross_turn_context:
            full_system_prompt = f"{tool_prompt}\n\n--- LỊCH SỬ HỘI THOẠI TRƯỚC ---\n{cross_turn_context}\n--- HẾT LỊCH SỬ ---"
        else:
            full_system_prompt = tool_prompt
        
        # Build final input messages
        input_messages = [
            SystemMessage(content=full_system_prompt),
            *current_turn_messages,
        ]
        
        # Log for debugging
        logger.debug(f"Cross-turn context length: {len(cross_turn_context)} chars")
        logger.debug(f"Current turn messages: {[type(m).__name__ for m in current_turn_messages]}")

        response = self.llm_tool_call.invoke(input_messages)
        
        # LOGGING
        logger.info(f"Tool generator raw response content: {response.content}")
        tool_calls = getattr(response, 'tool_calls', [])
        logger.info(f"Tool generator tool_calls: {tool_calls}")
        

        # FALLBACK: If model returns NOTHING (Empty text, No tools), force a response
        if not response.content and not tool_calls:
            logger.warning("Model returned empty response. Forcing a summary.")
            fallback_text = "Tôi đã kiểm tra dữ liệu nhưng có vẻ không tìm thấy thông tin cụ thể hoặc đã hoàn thành tác vụ. Bạn cần giúp gì thêm không?"
            response = AIMessage(content=fallback_text)
        
        return {
            'messages': [response],
            'iteration_count': current_iteration + 1
        }
    
    def take_action(self, state: AgentState) -> dict:
        """Execute tool calls from the last message."""
        last_message = state['messages'][-1]
        
        if not last_message or not hasattr(last_message, 'tool_calls') or not last_message.tool_calls:
            return {'messages': []}
            
        tool_messages = []
        for tool_call in last_message.tool_calls:
            tool_name = tool_call['name']
            tool_args = tool_call['args']
            tool_id = tool_call['id']
            
            logger.info(f"Executing: {tool_name}")
            logger.info(f"Args: {tool_args}")
            
            # Execute tool
            if tool_name in self.tools:
                try:
                    result = self.tools[tool_name].invoke(tool_args)
                    logger.info(f"Success: {result}")
                    
                    tool_messages.append(ToolMessage(
                        content=json.dumps(result, ensure_ascii=False, default=str),
                        tool_call_id=tool_id,
                        name=tool_name
                    ))
                    
                except Exception as e:
                    logger.error(f"Error: {str(e)}")
                    tool_messages.append(ToolMessage(
                        content=f"Error: {str(e)}",
                        tool_call_id=tool_id,
                        name=tool_name
                    ))
            else:
                tool_messages.append(ToolMessage(
                    content=f"Unknown tool: {tool_name}",
                    tool_call_id=tool_id,
                    name=tool_name
                ))
                    
        return {'messages': tool_messages}

    # Direct answer node
    def direct_generator(self, state: AgentState) -> dict:
        """Generate direct answer for non-tool-related queries."""
        
        raw_history = state['messages'][-10:]
        query = state['query']
        system_prompt = """Bạn là trợ lý AI hữu ích và thân thiện.
NHIỆM VỤ: Trả lời các câu hỏi giao tiếp thông thường, viết email, hoặc giải thích các khái niệm chung.
NGUYÊN TẮC:
- Trả lời ngắn gọn, tự nhiên.
- Nếu câu hỏi liên quan đến dữ liệu dự án cụ thể mà bạn không biết, hãy gợi ý người dùng hỏi rõ hơn để dùng công cụ tra cứu.
- KHÔNG bịa đặt dữ liệu dự án."""

        # Sanitize history to avoid Gemini 400 errors with tool_calls structures
        sanitized_history = []
        for msg in raw_history:
            if isinstance(msg, HumanMessage):
                sanitized_history.append(msg)
            elif isinstance(msg, AIMessage):
                # Only include content, strip tool_calls to avoid API errors
                if msg.content:
                    sanitized_history.append(HumanMessage(content=f"[Previous AI response]: {msg.content}"))
            # Skip ToolMessage for direct generator - not relevant

        input_messages = [
            SystemMessage(content=system_prompt),
            *sanitized_history,
            HumanMessage(content=query)
        ]
        
        response = self.llm_direct.invoke(input_messages)
        
        logger.info(f"Direct generator response: {response.content}")
        
        return {"messages": [response]}
        
    # Conditions
    def _intent_classify(self, state: AgentState):
        return state['router_decision']
    
    def _exist_tool(self, state: AgentState) -> bool:
        messages = state.get('messages', [])
        if not messages:
            return False
        
        last_message = messages[-1]
        tool_calls = getattr(last_message, 'tool_calls', None)
        return bool(tool_calls)

    def get_graph(self):
        """Hiển thị graph dưới dạng hình ảnh"""
        from IPython.display import Image, display
        
        img = self.graph.get_graph().draw_mermaid_png()
        return display(Image(img))