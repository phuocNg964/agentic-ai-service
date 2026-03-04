"""
Schemes for Meeting-to-Task Agent
"""
from typing import Literal, TypedDict, List, Optional
from pydantic import BaseModel, Field


class ActionItem(BaseModel):
    """
    Schema for an action item - matches backend POST /tasks input.
    """
    title: str = Field(..., description="Tiêu đề task, mô tả ngắn gọn công việc cần làm")
    description: Optional[str] = Field(None, description="Mô tả chi tiết về task, context và yêu cầu cụ thể")
    assignee: Optional[str] = Field(None, description="Tên người được giao task (phải nằm trong danh sách participants)")
    priority: Optional[str] = Field(None, description="Độ ưu tiên: Low, Medium, High")
    due_date: Optional[str] = Field(None, description="Deadline của task, định dạng ISO: YYYY-MM-DD (ví dụ: 2025-12-15)")
    status: Optional[str] = Field("To Do", description="Trạng thái task: To Do, In Progress, Done")
    tags: Optional[str] = Field(None, description="Tags phân loại task, phân cách bằng dấu phẩy")


class ReflectionOutput(BaseModel): 
    """Schema for output of reflection node"""
    critique: str = Field(..., description="Đánh giá chi tiết về chất lượng, liệt kê các vấn đề và đề xuất cải thiện")
    decision: Literal['accept', 'revise'] = Field(..., description="Quyết định: 'accept' nếu đạt chất lượng, 'revise' nếu cần chỉnh sửa")


class MeetingOutput(BaseModel):
    """Schema for output of meeting analysis node"""
    summary: str = Field(..., description="Tóm tắt cuộc họp bao gồm: mục đích, nội dung thảo luận chính, các quyết định đưa ra")
    action_items: List[ActionItem] = Field(..., description="Danh sách các công việc cần thực hiện sau cuộc họp")


class AgentState(TypedDict):
    """
    AgentState stores information throughout the Meeting-to-Task Agent workflow.
    """
    # Input
    audio_file_path: str
    meeting_metadata: Optional[dict]
    
    # Processing
    transcript: str
    summary: str
    action_items: List[dict]
    
    # Reflection
    reflect_decision: str
    critique: str
    
    # Completion
    tasks_created: List[dict]
    notification_sent: List[dict]
    
    # Control flow
    revision_count: int
    max_revisions: int
