"""Task management router stubs.

This module will be fully implemented by Issue #2 (Task Runner & Queue Service).
"""

import uuid
from typing import List, Optional
from datetime import datetime, timezone
from fastapi import (
    APIRouter,
    UploadFile,
    File,
    Form,
    Query,
    HTTPException,
    status,
    Response,
)
from fastapi.responses import StreamingResponse
from server.schemas import (
    TaskCreateResponse,
    TaskResponse,
    TaskListResponse,
    TaskCancelResponse,
    ErrorResponse,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post(
    "",
    response_model=List[TaskCreateResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Batch upload books and create translation tasks",
)
async def create_tasks(
    files: List[UploadFile] = File(..., description="One or more book files"),
    target_lang: Optional[str] = Form(None, description="Target language override"),
    provider: Optional[str] = Form(None, description="Model provider override"),
    prompt: Optional[str] = Form(None, description="Custom prompt template"),
) -> List[TaskCreateResponse]:
    """Stub endpoint for creating tasks from uploaded book files."""
    created_tasks = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for uploaded_file in files:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        created_tasks.append(
            TaskCreateResponse(
                id=task_id,
                filename=uploaded_file.filename or "unknown.epub",
                status="pending",
                created_at=now_iso,
            )
        )
    return created_tasks


@router.get(
    "",
    response_model=TaskListResponse,
    summary="List translation tasks with optional filtering and pagination",
)
async def list_tasks(
    status_filter: str = Query("all", alias="status", description="Filter by status: pending, running, completed, failed, cancelled, all"),
    limit: int = Query(50, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> TaskListResponse:
    """Stub endpoint for listing translation tasks."""
    return TaskListResponse(total=0, items=[])


@router.post(
    "/{task_id}/cancel",
    response_model=TaskCancelResponse,
    summary="Cancel a queued or running task",
)
async def cancel_task(task_id: str) -> TaskCancelResponse:
    """Stub endpoint for cancelling a task."""
    return TaskCancelResponse(id=task_id, status="cancelled")


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete task record and optionally associated files",
)
async def delete_task(
    task_id: str,
    delete_file: bool = Query(True, description="Whether to also remove physical files on disk"),
) -> Response:
    """Stub endpoint for deleting task."""
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{task_id}/logs/stream",
    summary="Stream real-time task logs using Server-Sent Events (SSE)",
)
async def stream_task_logs(task_id: str) -> StreamingResponse:
    """Stub endpoint for SSE log streaming."""
    async def event_generator():
        yield f'data: {{"time": "00:00:00", "line": "Log stream connected for {task_id}."}}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{task_id}/download",
    summary="Download bilingual translated output file",
)
async def download_task_output(task_id: str):
    """Stub endpoint for downloading translated product file."""
    # Raise 404 in stub since no real output exists yet
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Task {task_id} output file not found or not completed yet.",
    )
