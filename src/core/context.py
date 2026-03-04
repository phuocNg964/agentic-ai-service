from contextvars import ContextVar
from typing import Optional

# Define a ContextVar to hold the Authorization token for the current request context.
# This is thread-safe and async-task-safe.
_request_token: ContextVar[Optional[str]] = ContextVar("request_token", default=None)

def set_request_token(token: str):
    """Set the token for the current context."""
    _request_token.set(token)

def get_request_token() -> Optional[str]:
    """Get the token from the current context."""
    return _request_token.get()
