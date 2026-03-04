from fastapi import APIRouter, HTTPException, BackgroundTasks, Header
from typing import Optional
from src.schemas.meeting import MeetingAnalyzeRequest, MeetingAnalyzeResponse, MeetingConfirmRequest
from src.agents.meeting_to_task.agent import MeetingToTaskAgent
from src.agents.meeting_to_task.tools import create_tasks
from src.core.context import set_request_token

router = APIRouter()

def run_meeting_agent(meeting_id: str, audio_path: str, transcript: str, metadata: dict, auth_token: Optional[str]):
    """Background task wrapper"""
    # Important: Set context var inside the background thread
    if auth_token:
        set_request_token(auth_token)
        
    agent = MeetingToTaskAgent()
    agent.run(
        audio_file_path=audio_path, 
        transcript=transcript, 
        meeting_metadata=metadata, 
        thread_id=meeting_id,
    )

@router.post("/analyze", response_model=MeetingAnalyzeResponse)
async def analyze_meeting(
    request: MeetingAnalyzeRequest, 
    background_tasks: BackgroundTasks,
    background: bool = True, # New parameter to control execution mode
    skip_review: bool = True, # New parameter to control Human-in-the-Loop
    authorization: Optional[str] = Header(None)
):
    """
    Trigger meeting analysis. 
    If background=True (default), runs in background and returns immediate 'processing'.
    If background=False, runs synchronously and returns final result.
    If skip_review=False AND background=False, stops before task creation and returns 'waiting_review'.
    """
    try:
        # LOGIC: If both transcript and summary exist, we assume processing is done -> SKIP
        if request.transcript and request.summary:
             return MeetingAnalyzeResponse(
                meeting_id=request.meeting_id,
                status="skipped",
                thread_id=request.meeting_id,
                summary="Skipped: content already processed",
                action_items=[] 
            )

        # Validate inputs for processing
        if not request.audio_file_path and not request.transcript:
            raise HTTPException(status_code=400, detail="Either audio_file_path or transcript is required (if summary is missing)")

        # Extract token
        token = authorization.replace("Bearer ", "") if authorization and authorization.startswith("Bearer ") else authorization

        # Construct full metadata for the Agent
        meeting_metadata = {
            "title": request.title,
            "description": request.description,
            "author_id": request.author_id,
            "project_id": request.project_id,
            "participants": [p.model_dump() for p in request.participants]
        }

        if background:
            # Start background task (Old Behavior)
            background_tasks.add_task(
                run_meeting_agent, 
                request.meeting_id, 
                request.audio_file_path,
                request.transcript,
                meeting_metadata, 
                token
            )
            
            return MeetingAnalyzeResponse(
                meeting_id=request.meeting_id,
                status="processing",
                thread_id=request.meeting_id,
                transcript="Analysis started in background..."
            )
        else:
            # Run Synchronously (New Behavior for Backend Integration)
            if token:
                set_request_token(token)
                
            agent = MeetingToTaskAgent()
            # Run agent and wait for result
            final_state, thread_config = agent.run(
                audio_file_path=request.audio_file_path, 
                transcript=request.transcript, 
                meeting_metadata=meeting_metadata, 
                thread_id=request.meeting_id,
            )
            
            # Check if we should Human Review
            if not skip_review:
                # Helper to map Name -> ID
                # We need to do this because the UI expects IDs in the dropdown, but AI returns Names.
                # Use the participants list to find the ID.
                mapped_items = []
                # Create a mapping dictionary for case-insensitive name/username -> ID
                name_to_id = {}
                for p in request.participants:
                    if p.name: name_to_id[p.name.lower()] = p.id
                    # if p.username: name_to_id[p.username.lower()] = p.id # Schema has no username

                for item in final_state.get("action_items", []):
                     # Copy item to avoid mutating original state if needed elsewhere (though state is ephemeral here)
                     new_item = item.copy()
                     assignee_name = new_item.get("assignee")
                     if assignee_name and assignee_name.lower() != "unassigned":
                         # Try to find ID
                         found_id = name_to_id.get(assignee_name.lower())
                         if found_id:
                             new_item["assignee"] = found_id
                         # If not found, keep as Name (UI might show it as unknown or text)
                     mapped_items.append(new_item)

                return MeetingAnalyzeResponse(
                    meeting_id=request.meeting_id,
                    status="waiting_review",
                    thread_id=request.meeting_id,
                    summary=final_state.get("summary"), 
                    action_items=mapped_items,
                    transcript=final_state.get("transcript")
                )
            
            final_actions = agent.continue_after_review(thread_config)
            
            # Update final_state with the results from the second phase
            if final_actions:
                final_state.update(final_actions)
            
            # Extract results from state
            return MeetingAnalyzeResponse(
                meeting_id=request.meeting_id,
                status="completed",
                thread_id=request.meeting_id,
                summary=final_state.get("summary"), 
                action_items=final_state.get("action_items", []),
                transcript=final_state.get("transcript") 
            )

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/confirm", response_model=MeetingAnalyzeResponse)
async def confirm_meeting(
    request: MeetingConfirmRequest,
    authorization: Optional[str] = Header(None)
):
    """
    Confirm analysis results and resume Agent workflow (create tasks).
    """
    try:
        # Pass token to context for tools to use
        token = authorization.replace("Bearer ", "") if authorization and authorization.startswith("Bearer ") else authorization
        if token:
            set_request_token(token)

        # Reconstruct updated items for the agent
        # The agent expects a list of dicts for action_items
        updated_action_items = [item.model_dump() for item in request.updated_action_items] if request.updated_action_items else []
        
        # Resume the agent
        agent = MeetingToTaskAgent()
        thread_config = {'configurable': {'thread_id': request.meeting_id}}
        
        # This will run the remaining nodes (create_tasks -> END)
        final_state_updates = agent.continue_after_review(
            thread=thread_config,
            updated_summary=request.updated_summary,
            updated_action_items=updated_action_items
        )
        
        # Return final result
        return MeetingAnalyzeResponse(
            meeting_id=request.meeting_id,
            status="completed",
            thread_id=request.meeting_id,
            summary=request.updated_summary, # Return the confirmed summary
            action_items=request.updated_action_items or [],
            transcript="" 
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
