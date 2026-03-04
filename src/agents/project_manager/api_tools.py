"""
API Tools - Live Data Access and Modifications
Gọi trực tiếp Backend API để truy vấn và thao tác dữ liệu
"""
from typing import Dict, Any, Optional, List
from datetime import datetime
import os
import requests
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# Backend API base URL (points to FastAPI backend in this workspace)
API_BASE_URL = os.environ.get('API_BASE_URL', 'http://localhost:8000/api')

from src.core.config import settings
from src.core.context import get_request_token

# Cache
_auth_token_cache: Optional[str] = None # Kept for backward compat if needed, but context is preferred


def _get_auth_headers() -> dict:
    """Return headers including Authorization using the request-scoped token."""
    headers = {"Content-Type": "application/json"}

    token = get_request_token()
    if token:
        headers["Authorization"] = f"Bearer {token}" if not token.startswith("Bearer ") else token
        return headers

    # Fallback to legacy env/cache if needed (optional)
    # print("[project_manager.api_tools] No request token found.")
    return headers

# HELPER FUNCTIONS
def _api_get(endpoint: str, params: Dict = None) -> Dict[str, Any]:
    """Helper để gọi GET API"""
    url = f"{API_BASE_URL}{endpoint}"
    try:
        response = requests.get(
            url,
            params=params,
            headers=_get_auth_headers(),
            timeout=30
        )
        if response.status_code == 200:
            return {"success": True, "data": response.json()}
        else:
            print(f"[API ERROR] GET {url} failed. Status: {response.status_code}, Body: {response.text}")
            return {"success": False, "error": f"API error ({response.status_code}): {response.text}"}
    except requests.RequestException as e:
        print(f"[API NETWORK ERROR] GET {url} failed: {e}")
        return {"success": False, "error": f"Network error: {e}"}

def _api_post(endpoint: str, data: Dict) -> Dict[str, Any]:
    """Helper để gọi POST API"""
    try:
        response = requests.post(
            f"{API_BASE_URL}{endpoint}",
            json=data,
            headers=_get_auth_headers(),
            timeout=30
        )
        if response.status_code == 201:
            return {"success": True, "data": response.json()}
        else:
            return {"success": False, "error": f"API error ({response.status_code}): {response.text}"}
    except requests.RequestException as e:
        return {"success": False, "error": f"Network error: {e}"}

def _api_patch(endpoint: str, data: Dict) -> Dict[str, Any]:
    """Helper để gọi PATCH API"""
    try:
        response = requests.patch(
            f"{API_BASE_URL}{endpoint}",
            json=data,
            headers=_get_auth_headers(),
            timeout=30
        )
        if response.status_code == 200:
            return {"success": True, "data": response.json()}
        else:
            return {"success": False, "error": f"API error ({response.status_code}): {response.text}"}
    except requests.RequestException as e:
        return {"success": False, "error": f"Network error: {e}"}

# INPUT SCHEMAS
class CreateTaskInput(BaseModel):
    """Schema for creating a new task"""
    title: str = Field(description="Tiêu đề task, ngắn gọn và rõ ràng")
    project_id: str = Field(description="ID của project chứa task")
    author_user_id: Optional[str] = Field(default=None, description="ID của user tạo task (Optional - Backend tự lấy từ token)")
    description: Optional[str] = Field(default=None, description="Mô tả chi tiết task")
    priority: Optional[str] = Field(default="Medium", description="Độ ưu tiên: Low, Medium, High")
    status: Optional[str] = Field(default="To Do", description="Trạng thái: To Do, In Progress, Done")
    due_date: Optional[str] = Field(default=None, description="Deadline format YYYY-MM-DD")
    assigned_user_id: Optional[str] = Field(default=None, description="ID user được giao task")

class UpdateTaskStatusInput(BaseModel):
    """Schema for updating task status"""
    task_id: str = Field(description="ID của task cần cập nhật")
    status: str = Field(description="Trạng thái mới: To Do, In Progress, Done")

# TASK TOOLS
@tool(args_schema=CreateTaskInput)
def create_task(
    title: str,
    project_id: str,
    author_user_id: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = "Medium",
    status: Optional[str] = "To Do",
    due_date: Optional[str] = None,
    assigned_user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Tạo task mới trong hệ thống.
    
    SỬ DỤNG KHI: Người dùng yêu cầu tạo/thêm task mới.
    VÍ DỤ: "Tạo task Review code", "Thêm task mới priority High"
    
    Args:
        title: Tiêu đề task
        project_id: ID project chứa task
        author_user_id: ID user tạo task (Optional)
        description: Mô tả chi tiết (optional)
        priority: Low/Medium/High (default: Medium)
        status: To Do/In Progress/Done (default: To Do)
        due_date: Deadline YYYY-MM-DD (optional)
        assigned_user_id: ID user được giao (optional)
    
    Returns:
        Task mới được tạo với ID
    """
    payload = {
        "title": title,
        "project_id": project_id,
        "description": description or "",
        "priority": priority,
        "status": status,
        "tags": [],  # Default empty list
        "due_date": due_date,
        "assignee_id": assigned_user_id,
    }
    # Chỉ gửi author_id nếu có (dù backend thường ignore và dùng token)
    if author_user_id:
        payload["author_id"] = author_user_id
    
    result = _api_post("/v1/tasks", payload)
    
    if result["success"]:
        task = result["data"]
        return {
            "success": True,
            "message": f"Task '{title}' đã được tạo thành công",
            "task": task
        }
    return result

@tool(args_schema=UpdateTaskStatusInput)
def update_task_status(task_id: str, status: str) -> Dict[str, Any]:
    """Cập nhật trạng thái của task.
    
    SỬ DỤNG KHI: Thay đổi status của task.
    VÍ DỤ: "Chuyển task #5 sang Done", "Mark task 10 as In Progress"
    
    Args:
        task_id: ID của task
        status: Trạng thái mới (To Do / In Progress / Review / Done)
    
    Returns:
        Task sau khi update
    """
    valid_statuses = ["To Do", "In Progress", "Done"]
    # Backend expects 'new_status' as query param for this specific endpoint
    # We pass empty body {} as the second argument to _api_patch
    result = _api_patch(f"/v1/tasks/{task_id}/status?new_status={status}", {})
    
    if result["success"]:
        task = result["data"]
        return {
            "success": True,
            "message": f"Task đã được cập nhật sang '{status}'",
            "task": task
        }
    return result

@tool
def get_user_projects() -> Dict[str, Any]:
    """Lấy danh sách các dự án mà người dùng hiện tại là thành viên.
    
    SỬ DỤNG KHI: Xem các project của user.
    VÍ DỤ: "Danh sách projects của tôi", "Các dự án tôi tham gia"
    
    Returns:
        List các projects user là member
    """
    result = _api_get("/v1/projects")
    
    if not result["success"]:
        return result
    
    projects = result["data"]
    return {
        "success": True,
        "total": len(projects),
        "projects": projects
    }

class GetProjectDetailsInput(BaseModel):
    project_id: str = Field(description="ID của dự án cần lấy thông tin")

@tool(args_schema=GetProjectDetailsInput)
def get_project_details(project_id: str) -> Dict[str, Any]:
    """Lấy thông tin chi tiết của một dự án.
    
    SỬ DỤNG KHI: User hỏi về chi tiết một dự án cụ thể (deadline, mô tả, members...).
    VÍ DỤ: "Chi tiết dự án A", "Ai là thành viên dự án X?"
    
    Args:
        project_id: ID của dự án
    """
    result = _api_get(f"/v1/projects/{project_id}")
    return result

class GetProjectTasksInput(BaseModel):
    project_id: str = Field(description="ID của dự án cần lấy danh sách task")

@tool(args_schema=GetProjectTasksInput)
def get_project_tasks(project_id: str) -> Dict[str, Any]:
    """Lấy danh sách task của một dự án cụ thể.
    
    SỬ DỤNG KHI: User muốn xem task trong 1 project nhất định.
    VÍ DỤ: "Các task của dự án A", "Project X có việc gì cần làm?"
    
    Args:
        project_id: ID của dự án
    """
    result = _api_get(f"/v1/tasks/{project_id}")
    
    if not result["success"]:
        return result
        
    tasks = result["data"]
    
    return {
        "success": True,
        "tasks": tasks,
    }

class GetProjectMeetingsInput(BaseModel):
    project_id: str = Field(description="ID của dự án cần lấy danh sách cuộc họp")

@tool(args_schema=GetProjectMeetingsInput)
def get_project_meetings(project_id: str) -> Dict[str, Any]:
    """Lấy danh sách các cuộc họp (meetings) của một dự án.
    
    SỬ DỤNG KHI: User muốn xem lịch họp, danh sách cuộc họp của dự án.
    VÍ DỤ: "Lịch họp của dự án A", "Dự án này có cuộc họp nào?"
    
    Args:
        project_id: ID của dự án
    """
    result = _api_get(f"/v1/meetings/{project_id}")
    
    if not result["success"]:
        return result
        
    meetings = result["data"]
    return {
        "success": True,
        "count": len(meetings),
        "meetings": meetings
    }

@tool
def get_current_user_info() -> Dict[str, Any]:
    """Lấy thông tin của người dùng hiện tại đang đăng nhập.
    
    SỬ DỤNG KHI: Cần biết thông tin chi tiết của user (id, name, email).
    VÍ DỤ: "Tôi là ai?", "Thông tin tài khoản của tôi"
    
    Returns:
        Thông tin user (id, username, email, name...)
    """
    result = _api_get("/v1/users/me")
    return result

# EXPORT ALL TOOLS
# Chỉ giữ lại các tools tương ứng với API thực tế
ALL_API_TOOLS = [
    get_current_user_info,
    get_user_projects,
    get_project_details,
    create_task,
    update_task_status,
    get_project_tasks,
    get_project_meetings,
]