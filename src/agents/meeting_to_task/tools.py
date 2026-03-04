"""
Tools for Meeting-to-Task Agent
"""
from typing import Dict, List, Optional
from datetime import datetime
import time
import smtplib
import os  # Added missing import
import requests
from google import genai
from google.genai import types
from faster_whisper import WhisperModel
from email.mime.text import MIMEText

from src.core.config import settings
from src.core.context import get_request_token

from src.core.logging import logger

# Cache
_stt_model_cache = {}


def _get_auth_headers() -> dict:
    """Return headers including Authorization using the request-scoped token."""
    headers = {"Content-Type": "application/json"}

    token = get_request_token()
    if token:
        # If the token already has 'Bearer ' prefix, use it as is, otherwise add it.
        # Usually from standard header it comes with Bearer, but let's be safe or just assume direct token?
        # Standard approach: extract raw token in API, store raw token, append 'Bearer' here.
        headers["Authorization"] = f"Bearer {token}" if not token.startswith("Bearer ") else token
        
    return headers

def transcribe_audio(audio_file_path: str, use_mock: bool = False, provider: str = 'gemini', participants: Optional[List[dict]] = None) -> str:
    """
    Convert audio file to text.
    """
    try:
        # NOTE: Mock logic preserved for dev/demo purposes but defaulted to False
        if use_mock:
            return "MOCK TRANSCRIPT: Meeting about project roadmap..."
                
        # In production this likely comes from S3 URL, not local path
        if not audio_file_path or not os.path.exists(audio_file_path):
             # Just a fallback check if it's a local path
             pass 
        
        cache_key = f"{provider}:{audio_file_path}"
        if cache_key in _stt_model_cache:
            return _stt_model_cache[cache_key]
        
        transcript = ""
        
        if provider == "faster-whisper":
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(audio_file_path, language="vi", beam_size=3)
            transcript = " ".join([segment.text for segment in segments])
            
        elif provider == "gemini":
            if not settings.google_key:
                raise ValueError("Missing Google API Key in settings")
                
            # --- NEW SDK MIGRATION (google-genai) ---
            # Create a client instance instead of global configuration
            client = genai.Client(api_key=settings.google_key)
            
            # Use 'files' service for upload (formerly genai.upload_file)
            # Depending on version, this might be client.files.upload or similar
            # Checking standard new pattern:
            
            logger.info(f"🚀 [Gemini] Uploading file: {audio_file_path}")
            # Correcting argument name from 'path' to 'file'
            upload_result = client.files.upload(file=audio_file_path)
            
            # Wait for processing (New SDK usually handles this or needs manual loop)
            # Assuming 'upload_result' has 'name' and we can poll 'get'
            file_name = upload_result.name
            
            while True:
                # Retrieve file status
                retrieved_file = client.files.get(name=file_name)
                state = retrieved_file.state.name # e.g. "PROCESSING", "ACTIVE"
                
                if state == "ACTIVE":
                    break
                elif state == "FAILED":
                    raise Exception("File upload failed.")
                    
                time.sleep(2)
            
            logger.info(f"✅ [Gemini] File ready. Generating content...")
            
            # Context regarding participants
            participants_context = ""
            if participants:
                names = [p.get('name', 'Unknown') for p in participants if p.get('name')]
                if names:
                    participants_context = f" Danh sách người tham gia: {', '.join(names)}."

            prompt_text = (
                f"Tạo bản ghi chép cuộc họp chính xác từng từ.{participants_context} "
                "Định dạng bắt buộc: [HH:MM:SS] Tên người nói: Nội dung hội thoại. Ngôn ngữ: Tiếng Việt."
            )

            # Generate Content using the Client
            response = client.models.generate_content(
                model='gemini-2.0-flash-lite', # Updated to latest or keep 1.5/2.0
                contents=[
                    types.Content(
                        parts=[
                            types.Part.from_text(text=prompt_text),
                            types.Part.from_uri(
                                file_uri=retrieved_file.uri,
                                mime_type=retrieved_file.mime_type
                            )
                        ]
                    )
                ]
            )
            transcript = response.text
        else:
            raise ValueError(f"Unsupported provider: {provider}")
        
        _stt_model_cache[cache_key] = transcript
        return transcript
        
    except Exception as e:
        raise Exception(f"Transcribe error: {e}")


def get_emails_from_participants(participants: List[dict]) -> Dict[str, str]:
    """Map username -> email"""
    emails = {}
    for participant in participants:
        name = participant.get('name')
        email = participant.get('email')
        uid = participant.get('id')
        if email:
            if name:
                emails[name.lower()] = email
            if uid:
                emails[str(uid)] = email
    return emails


def send_notification(
    email_body: str,
    receiver_email: str,
    subject: str = "Meeting Summary",
) -> bool:
    """Send email notification."""
    try:
        if not email_body:
            return False
        
        sender_email = settings.EMAIL_SENDER
        sender_password = settings.EMAIL_PASSWORD
        
        if not sender_email or not sender_password:
            logger.warning(f"⚠️ Preview mode (Missing EMAIL config in settings)")
            return True
        
        if not receiver_email:
            return False
        
        msg = MIMEText(email_body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = receiver_email
        
        with smtplib.SMTP('smtp.gmail.com', 587) as smtp:
            smtp.starttls()
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
        
        return True
    except Exception as e:
        logger.error(f"❌ Email failed: {e}")
        return False


def format_email_body_for_assignee(
    assignee_name: str,
    assignee_task: dict,
    summary: str,
    meeting_metadata: dict
) -> str:
    """Format email body."""
    meeting_title = meeting_metadata.get('title', 'Meeting')
    task_title = assignee_task.get('title', 'Task')
    
    return f"Xin chào {assignee_name},\n\nBạn có công việc mới từ cuộc họp '{meeting_title}':\n\n- {task_title}\n\nTóm tắt cuộc họp (Summary):\n{summary}"


def create_tasks(
    action_items: List[dict],
    project_id: int,
    author_user_id: int,
    user_mapping: Optional[Dict[str, int]] = None
) -> List[dict]:
    """
    Create tasks via Backend API.
    """
    if not action_items:
        return []
    
    created_tasks = []
    user_mapping = user_mapping or {}
    api_url = f"{settings.API_BASE_URL.rstrip('/')}/v1/tasks"
    
    for item in action_items:
        # Simplified Logic for brevity
        try:
            # Prepare fields
            assignee_val = item.get("assignee", "")
            assigned_user_id = None
            
            # Check if assignee_val is already an ID (present in mapping values)
            valid_ids = set(user_mapping.values())
            if assignee_val in valid_ids:
                assigned_user_id = assignee_val
            else:
                 # Try to map from name
                 assigned_user_id = user_mapping.get(assignee_val.lower())
            
            item_tags = []
            if item.get("tags"):
                item_tags = [t.strip() for t in item.get("tags").split(",")]

            payload = {
                "title": item.get("title", "Untitled Task"),
                "project_id": project_id,
                "author_id": author_user_id, # Schema matches 'author_id'
                "description": item.get("description"),
                "status": item.get("status", "To Do"),
                "priority": item.get("priority") or "Medium",  # Handle null priority
                "tags": item_tags,
                "due_date": item.get("due_date"),
                "assignee_id": assigned_user_id, # CORRECTED field name
            }
            
            response = requests.post(
                api_url,
                json=payload,
                headers=_get_auth_headers(),
                timeout=10
            )
            
            if response.status_code == 201:
                created_tasks.append(response.json())
            else:
                logger.error(f"Failed to create task: {response.text}")
                
        except Exception as e:
            logger.error(f"Error creating task: {e}")
            
    return created_tasks