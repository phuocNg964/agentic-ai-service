import logging
import json
import warnings
from typing import List, Optional
import os # Ensure os is imported as it is used
from pathlib import Path

# from dotenv import load_dotenv

# Suppress Pydantic deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydantic")

# LangGraph và LangChain
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage

# Import từ module này
from .schemas import AgentState, MeetingOutput, ReflectionOutput, ActionItem
from .prompts import ANALYSIS_PROMPT, REFLECTION_PROMPT, REFINEMENT_PROMPT
from .tools import (
    format_email_body_for_assignee, 
    get_emails_from_participants, 
    transcribe_audio, 
    create_tasks, 
    send_notification
)
from ...models.models import call_llm

logger = logging.getLogger(__name__)

# Global memory checkpointer to persist state across instances (requests)
_memory_store = MemorySaver()

class MeetingToTaskAgent:
    """
    Agent xử lý meeting recordings và tạo tasks tự động
    """
    
    def __init__(self):
        """
        Khởi tạo agent
    
        Args:
            provider_name: Tên provider LLM để sử dụng
        """
        self.model = call_llm(
            model_provider='gemini',
            model_name='gemini-2.5-flash',
            temperature=0.1,
            top_p=0.3,
        )
        self.memory = _memory_store # Use global instance
        self.graph = self._build_graph()
    
    def _build_graph(self) -> StateGraph:
        """Xây dựng workflow graph"""
        builder = StateGraph(AgentState)
        
        # Thêm các nodes
        builder.add_node('stt', self._stt)
        builder.add_node('analysis', self._analysis)
        builder.add_node('reflection', self._reflection)
        builder.add_node('refinement', self._refinement)
        builder.add_node('create_tasks', self._create_tasks)
        builder.add_node('notification', self._notification)
        
        # Thiết lập entry point
        builder.set_entry_point('stt')
        
        # Thêm các edges
        builder.add_edge('stt', 'analysis')
        builder.add_edge('analysis', 'reflection')
        
        # Conditional edge: reflection -> refine hoặc create_tasks
        builder.add_conditional_edges(
            'reflection',
            self._should_create_tasks,
            {
                False: 'refinement',
                True: 'create_tasks'
            }
        )
        
        # Edge: refinement quay lại reflection để kiểm tra lại
        builder.add_edge('refinement', 'reflection')
        
        # Edge: create_tasks -> notification -> END
        builder.add_edge('create_tasks', 'notification')
        builder.add_edge('notification', END)
        # Compile graph với memory và interrupt_before
        return builder.compile(
            checkpointer=self.memory,
            interrupt_before=['create_tasks']
        )
    
    # ==================== NODES ====================
    
    def _stt(self, state: AgentState):
        """Node 1: Chuyển đổi âm thanh thành văn bản"""
        logger.info("\n[NODE 1] Speech-to-Text")
        
        # Check if transcript is already provided in state (e.g. from API)
        if state.get('transcript'):
            transcript = state['transcript']
            logger.info(f"Using provided transcript")
        else:
            participants = state.get('meeting_metadata', {}).get('participants', [])
            transcript = transcribe_audio(
                state['audio_file_path'], 
                provider='gemini', 
                use_mock=False,
                participants=participants
            )
        
        # Log length and preview
        logger.info(f"Transcript length: {len(transcript)} characters")
        preview = transcript[:200].replace('\n', ' ').strip()
        logger.info(f"Preview: {preview}...")
        
        return {'transcript': transcript}
    
    def _analysis(self, state: AgentState):
        """Node 2: Phân tích và tạo Summary + Action Items"""
        logger.info("\n[NODE 2] Meeting Analysis")
        
        metadata_str = json.dumps(state.get('meeting_metadata', {}), indent=2, ensure_ascii=False)
        
        messages = [
            HumanMessage(content=ANALYSIS_PROMPT.format(
                metadata=metadata_str,
                transcript=state['transcript']
            ))
        ]
        
        response = self.model.with_structured_output(MeetingOutput).invoke(messages)
        
        if response is None:
            raise ValueError("LLM failed to generate structured output for meeting analysis")
        
        if not hasattr(response, 'action_items') or response.action_items is None or response.summary is None:
            raise ValueError("LLM response missing required fields (summary or action_items)")
        
        # Chuyển đổi action items sang dict
        action_items_list = [item.model_dump() for item in response.action_items]
        
        # Log Summary
        logger.info(f"Summary:\n{response.summary}")
        
        # Log Tasks (pretty print)
        logger.info(f"Tasks ({len(action_items_list)}):")
        logger.info(json.dumps(action_items_list, indent=2, ensure_ascii=False))
        
        return {
            'summary': response.summary,
            'action_items': action_items_list,
        }
    
    def _reflection(self, state: AgentState):
        """Node 3: Tự kiểm tra và phát hiện lỗi"""
        logger.info("\n[NODE 3] Quality Check")
        
        # Pass the entire metadata object to the prompt for context.
        metadata_str = json.dumps(state.get('meeting_metadata', {}), indent=2, ensure_ascii=False)
        action_items_str = json.dumps(state['action_items'], indent=2, ensure_ascii=False)
        # Get schema definition for validation anchor
        schema_str = json.dumps(ActionItem.model_json_schema(), indent=2, ensure_ascii=False)

        # Simplified Participants List for Anchor
        participants = state.get('meeting_metadata', {}).get('participants', [])
        p_names = [p.get('name', '') for p in participants if p.get('name')]
        participants_str = ", ".join(p_names)

        messages = [
            HumanMessage(content=REFLECTION_PROMPT.format(
                metadata=metadata_str,
                participants_list=participants_str,
                transcript=state['transcript'],
                schema=schema_str,
                summary=state['summary'],
                action_items=action_items_str
            ))
        ]
        
        response = self.model.with_structured_output(ReflectionOutput).invoke(messages)
        
        # Error handling for None response
        if response is None:
            logger.error("Reflection: LLM returned None response")
            return {'critique': 'LLM failed to respond', 'reflect_decision': 'accept'}
        
        # Log decision and critique
        logger.info(f"Decision: {response.decision}")
        logger.info(f"Critique: {response.critique}")

        return {'critique': response.critique, 'reflect_decision': response.decision}
    
    def _refinement(self, state: AgentState):
        """Node 4: Tinh chỉnh dựa trên phản hồi"""
        revision_count = state.get('revision_count', 0) + 1
        logger.info(f"\n[NODE 4] Refinement (Revision #{revision_count})")
        
        # Pass the entire metadata object to the prompt for context.
        metadata_str = json.dumps(state.get('meeting_metadata', {}), indent=2, ensure_ascii=False)
        action_items_str = json.dumps(state['action_items'], indent=2, ensure_ascii=False)
        
        messages = [
            HumanMessage(content=REFINEMENT_PROMPT.format(
                metadata=metadata_str,
                draft_summary=state['summary'],
                draft_action_items=action_items_str,
                critique=state['critique'],
                transcript=state['transcript']
            ))
        ]
        
        response = self.model.with_structured_output(MeetingOutput).invoke(messages)
        
        # Error handling for None response
        if response is None:
            logger.error("Refinement: LLM returned None response, keeping original")
            return {'revision_count': revision_count}
        
        refined_action_items = [item.model_dump() for item in response.action_items]
        
        # Log refined Summary and Tasks
        logger.info(f"Refined Summary:\n{response.summary}")
        logger.info(f"Refined Tasks ({len(refined_action_items)}):")
        logger.info(json.dumps(refined_action_items, indent=2, ensure_ascii=False))
        
        return {
            'summary': response.summary,
            'action_items': refined_action_items,
            'revision_count': revision_count
        }
    
    def _create_tasks(self, state: AgentState):
        """Node 5: Tạo tasks trong hệ thống backend"""
        logger.info("\n[NODE 5] Create Tasks")
        action_items = state.get('action_items', [])
        meeting_metadata = state.get('meeting_metadata', {})
        participants = meeting_metadata.get('participants', [])
        
        # Extract project_id and author_user_id from meeting metadata
        project_id = meeting_metadata.get('project_id')
        author_user_id = meeting_metadata.get('author_id')
        
        user_mapping = {}
        for p in participants:
            # Try multiple keys for name/username
            username = p.get('name') or p.get('username') or ''
            user_id = p.get('id') or p.get('userId')
            
            if username and user_id:
                user_mapping[username.lower().strip()] = user_id
        
        # Call API to create tasks
        tasks = create_tasks(
            action_items=action_items,
            project_id=project_id,
            author_user_id=author_user_id,
            user_mapping=user_mapping
        )
        
        # Log status
        if tasks:
            successful = len([t for t in tasks if t.get('status') != 'error'])
            logger.info(f"Status: Successful - Created {successful}/{len(tasks)} tasks")
        else:
            logger.info(f"Status: Failed - No tasks created")
        
        return {'tasks_created': tasks}
    
    def _notification(self, state: AgentState):
        """Node 6: Gửi thông báo tới từng assignee"""
        logger.info("\n[NODE 6] Send Notifications")
        
        summary = state.get('summary')
        action_items = state.get('action_items', [])
        meeting_metadata = state.get('meeting_metadata', {})
        participants = meeting_metadata.get('participants', [])
        
        # Lấy email mapping từ participants
        email_map = get_emails_from_participants(participants)
        
        # Gửi email cho từng task
        results = []
        emails_sent = []
        
        for task in action_items:
            assignee = (task.get('assignee') or '').lower()
            
            # Skip nếu là Unassigned
            if assignee == 'unassigned' or not assignee:
                continue
            
            email = email_map.get(assignee)
            
            if not email:
                results.append({
                    "assignee": assignee,
                    "email": None,
                    "title": task.get('title', ''),
                    "status": "skipped",
                    "reason": "Email not found in participants"
                })
                continue
            
            # Format email riêng cho task này
            # Resolve assignee name nicely
            display_name = assignee.title()
            for p in participants:
                # If assignee matches ID, use Name
                if str(p.get('id')) == assignee or str(p.get('userId')) == assignee:
                    if p.get('name'):
                        display_name = p.get('name')
                        break
            
            email_body = format_email_body_for_assignee(
                assignee_name=display_name,
                assignee_task=task,
                summary=summary,
                meeting_metadata=meeting_metadata
            )
            
            result = send_notification(
                email_body=email_body,
                receiver_email=email,
                subject=f"[Action Required] {meeting_metadata.get('title', 'Meeting')} - Công việc cho {assignee.title()}"
            )
            
            if result:
                emails_sent.append(email)
            
            results.append({
                "assignee": assignee,
                "email": email,
                "title": task.get('title', ''),
                "status": "sent" if result else "failed"
            })
        
        # Log number of emails sent and list of emails
        sent_count = len([r for r in results if r['status'] == 'sent'])
        logger.info(f"Emails sent: {sent_count}")
        if emails_sent:
            logger.info(f"Sent to: {', '.join(emails_sent)}")
        
        return {'notification_sent': results}
    
    # ==================== CONDITIONAL LOGIC ====================
    
    def _should_create_tasks(self, state: AgentState) -> bool:
        """Quyết định có cần tinh chỉnh dựa trên critique không."""
        decision = state.get('reflect_decision', '')
        max_revisions = state.get('max_revisions', 2)
        revision_count = state.get('revision_count', 0)
        
        # Accept nếu decision là accept HOẶC đã đạt max revisions
        if decision == 'accept':
            return True
        if revision_count >= max_revisions:
            logger.info(f"  ⚠️ Đạt max revisions ({max_revisions}), tiếp tục...")
            return True
        return False
    
    # ==================== PUBLIC METHODS ====================
    
    def run(self, audio_file_path: str, meeting_metadata: Optional[dict] = None, 
            max_revisions: int = 1, thread_id: str = '1', transcript: Optional[str] = None):
        """
        Chạy workflow đến điểm Human Review
        
        Args:
            audio_file_path: Đường dẫn đến file âm thanh
            meeting_metadata: Metadata của cuộc họp (bao gồm participants)
            max_revisions: Số lần tối đa cho phép tinh chỉnh
            thread_id: ID của thread cho memory
            transcript: (Optional) Transcript text if available
            
        Returns:
            Tuple[dict, dict]: (current_state values, thread_config)
        """
        initial_state = {
            'audio_file_path': audio_file_path,
            'meeting_metadata': meeting_metadata or {},
            'max_revisions': max_revisions,
            'revision_count': 0,
            'transcript': transcript,
        }
        
        thread = {'configurable': {'thread_id': thread_id}}
        
        logger.info("\n🚀 Starting Meeting-to-Task Agent...")
        logger.info("="*100)
        
        # Chạy đến điểm interrupt
        for event in self.graph.stream(initial_state, thread):
            pass  # Events đã được print trong nodes

        current_state = self.graph.get_state(thread)
        return current_state.values, thread
    
    def continue_after_review(self, thread, updated_summary: str = None, 
                              updated_action_items: list = None):
        """Cập nhật state và tiếp tục workflow sau human review"""
        if updated_summary or updated_action_items:
            updates = {}
            if updated_summary:
                updates['summary'] = updated_summary
            if updated_action_items:
                updates['action_items'] = updated_action_items
            self.graph.update_state(thread, updates)
        
        logger.info("\n▶️ Continuing after human review...")
        
        for event in self.graph.stream(None, thread):
            pass
        
        final_state = self.graph.get_state(thread)
        return final_state.values
    
    def get_graph(self):
        """Hiển thị graph dưới dạng hình ảnh"""
        from IPython.display import Image, display
        
        img = self.graph.get_graph().draw_mermaid_png()
        return display(Image(img))