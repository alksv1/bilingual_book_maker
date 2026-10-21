"""Persistent task management and disk-backed SSE logs."""

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import aiofiles
import aiosqlite
from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from book_maker.prompt_file import check_user_placeholders
from server import config
from server.models import FileType, TaskStatus
from server.schemas import (
    TaskCancelResponse,
    TaskCreateResponse,
    TaskListResponse,
    TaskResponse,
)
from server.services.runner import fetch_task, read_general_settings, runner


@asynccontextmanager
async def lifespan(app):
    # The application's lifespan initializes the database before router lifespans.
    await runner.start()
    try:
        yield
    finally:
        await runner.stop()


router = APIRouter(prefix="/api/tasks", tags=["tasks"], lifespan=lifespan)
ALLOWED_EXTENSIONS = {f".{kind.value}" for kind in FileType}
TERMINAL = {"completed", "failed", "cancelled"}
LOG_TIMESTAMP = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] (.*)$")


def managed_path(value, root):
    """Never serve or delete files outside the service's managed directories."""
    path = Path(value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise HTTPException(400, "Task file is outside its managed directory")
    return path


async def require_task(task_id):
    task = await fetch_task(task_id)
    if task is None:
        raise HTTPException(404, f"Task {task_id} not found")
    return task


@router.post("", response_model=List[TaskCreateResponse], status_code=201)
async def create_tasks(
    files: List[UploadFile] = File(...),
    target_lang: Optional[str] = Form(None),
    provider: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
) -> List[TaskCreateResponse]:
    """Persist a complete upload batch before making any task runnable."""
    paths = []
    created = []
    records = []
    committed = False
    try:
        if prompt:
            try:
                if "{text}" not in prompt:
                    raise ValueError(
                        "Custom prompt must contain `{text}` for the source text."
                    )
                check_user_placeholders(prompt)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        names = [
            (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
            for file in files
        ]
        if not files or any(
            Path(name).suffix.lower() not in ALLOWED_EXTENSIONS for name in names
        ):
            raise HTTPException(400, "Supported file formats: epub, pdf, txt, srt")
        general = await read_general_settings()
        language = target_lang or general.get("default_target_lang", "zh-hans")
        if not isinstance(language, str) or not language.strip():
            raise HTTPException(400, "Target language must not be blank")
        provider = provider or general.get("active_provider")
        now = datetime.now(timezone.utc).isoformat()
        for root in (config.UPLOADS_DIR, config.OUTPUT_DIR, config.LOGS_DIR):
            root.mkdir(parents=True, exist_ok=True)
        for file, name in zip(files, names):
            task_id = f"task_{uuid.uuid4().hex}"
            ext = Path(name).suffix.lower()
            source = config.UPLOADS_DIR / f"{task_id}{ext}"
            paths.append(source)
            async with aiofiles.open(source, "xb") as destination:
                while chunk := await file.read(65536):
                    await destination.write(chunk)
            if source.stat().st_size == 0:
                raise HTTPException(400, "Uploaded books must not be empty")
            output_ext = ".epub" if ext == ".pdf" else ext
            output = config.OUTPUT_DIR / f"{task_id}_bilingual{output_ext}"
            log = config.LOGS_DIR / f"{task_id}.log"
            records.append(
                (
                    task_id,
                    name,
                    str(source),
                    str(output),
                    ext[1:],
                    provider,
                    language,
                    prompt,
                    str(log),
                    now,
                    now,
                )
            )
            created.append(
                TaskCreateResponse(id=task_id, filename=name, created_at=now)
            )
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.executemany(
                """INSERT INTO tasks
                (id, filename, file_path, output_path, file_type, model_provider,
                 target_language, prompt_template, log_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                records,
            )
            await db.commit()
        committed = True
        for task in created:
            await runner.enqueue(task.id)
        return created
    finally:
        if not committed:
            for path in paths:
                path.unlink(missing_ok=True)
        for file in files:
            await file.close()


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    status_filter: str = Query("all", alias="status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> TaskListResponse:
    if status_filter != "all" and status_filter not in {
        state.value for state in TaskStatus
    }:
        raise HTTPException(422, "Invalid task status")
    where = "" if status_filter == "all" else " WHERE status = ?"
    params = () if status_filter == "all" else (status_filter,)
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        async with db.execute("SELECT COUNT(*) FROM tasks" + where, params) as cursor:
            total = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT * FROM tasks"
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cursor:
            items = [TaskResponse(**dict(row)) for row in await cursor.fetchall()]
    return TaskListResponse(total=total, items=items)


@router.post("/{task_id}/cancel", response_model=TaskCancelResponse)
async def cancel_task(task_id: str) -> TaskCancelResponse:
    try:
        await runner.cancel_task(task_id)
    except KeyError:
        raise HTTPException(404, f"Task {task_id} not found") from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return TaskCancelResponse(id=task_id)


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str, delete_file: bool = Query(True)) -> Response:
    async with runner.lock:
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise HTTPException(404, f"Task {task_id} not found")
            if row["status"] == "running" or runner.is_active(task_id):
                raise HTTPException(409, "Cancel the running task before deleting it")
            if delete_file:
                for key, root in (
                    ("file_path", config.UPLOADS_DIR),
                    ("output_path", config.OUTPUT_DIR),
                    ("log_path", config.LOGS_DIR),
                ):
                    if row[key]:
                        managed_path(row[key], root).unlink(missing_ok=True)
                source = managed_path(row["file_path"], config.UPLOADS_DIR)
                # Known CLI artifacts only; never delete by a filename substring.
                for name in (
                    f".{source.stem}.temp.bin",
                    f"{source.stem}_plan.json",
                    f"{source.stem}_bilingual{source.suffix}",
                    f"{source.stem}_bilingual.epub",
                ):
                    candidate = source.with_name(name)
                    if candidate.is_file():
                        managed_path(candidate, config.UPLOADS_DIR).unlink(
                            missing_ok=True
                        )
            await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            await db.commit()
    return Response(status_code=204)


@router.get("/{task_id}/logs/stream")
async def stream_task_logs(task_id: str) -> StreamingResponse:
    task = await require_task(task_id)
    log_path = managed_path(task["log_path"], config.LOGS_DIR)

    async def events():
        # One disk cursor covers replay and live output without a subscribe gap
        # or an unbounded queue per slow client.
        position = 0
        terminal = False
        while True:
            if log_path.is_file():
                async with aiofiles.open(
                    log_path, encoding="utf-8", errors="replace"
                ) as stream:
                    await stream.seek(position)
                    while line := await stream.readline():
                        position = await stream.tell()
                        line = line.rstrip("\r\n")
                        match = LOG_TIMESTAMP.match(line)
                        timestamp, text = match.groups() if match else ("", line)
                        yield "data: " + json.dumps(
                            {"time": timestamp, "line": text}, ensure_ascii=False
                        ) + "\n\n"
            if terminal:
                return
            current = await fetch_task(task_id)
            if current is None or current["status"] in TERMINAL:
                # Terminal status is published after the final log flush. Drain
                # again in case the task finished during the preceding read.
                terminal = True
                continue
            yield ": keepalive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/download")
async def download_task_output(task_id: str):
    task = await require_task(task_id)
    if task["status"] != "completed" or not task["output_path"]:
        raise HTTPException(404, "Translated output is not ready")
    output = managed_path(task["output_path"], config.OUTPUT_DIR)
    if not output.is_file():
        raise HTTPException(404, "Translated output file is missing")
    filename = f"{Path(task['filename']).stem}_bilingual{output.suffix}"
    media_type = {
        ".epub": "application/epub+zip",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".srt": "text/plain",
    }.get(output.suffix, "application/octet-stream")
    return FileResponse(output, filename=filename, media_type=media_type)
