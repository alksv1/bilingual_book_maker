"""In-process task queue; the database and append-only logs are the public state."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import re
import shutil
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
import aiosqlite

from server import config
from server.services.settings import settings_service

logger = logging.getLogger(__name__)
PROGRESS_BRACKET_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")
PROGRESS_TRANSLATING_RE = re.compile(r"translating\s+(\d+)\s*/\s*(\d+)", re.I)
PROGRESS_TQDM_RE = re.compile(r"(?<![\d.])(\d+)\s*/\s*(\d+)\s*(?:\[|:)")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
MAX_FRAME = 16384
TERMINATE_TIMEOUT = 3.0
GENERAL_DEFAULTS = {
    "default_target_lang": "zh-hans",
    "bilingual_style": "default",
    "concurrency_limit": 1,
    "max_retries": 3,
    "active_provider": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_task(task_id: str, db_path: Path) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def fetch_task(task_id: str) -> dict | None:
    """Read from the live runner's pinned database, or current config when stopped."""
    return await runner.fetch_task(task_id)


async def read_general_settings(db_path: Path | None = None) -> dict:
    """Defaults < general.json < app_settings general blob < individual keys."""
    settings = dict(GENERAL_DEFAULTS)
    try:
        async with aiofiles.open(config.GENERAL_CONFIG_PATH, encoding="utf-8") as file:
            value = json.loads(await file.read())
        if isinstance(value, dict):
            settings.update({k: v for k, v in value.items() if k in settings})
    except (OSError, ValueError):
        pass
    try:
        async with aiosqlite.connect(db_path or config.DB_PATH) as db:
            async with db.execute("SELECT key, value FROM app_settings") as cursor:
                rows = dict(await cursor.fetchall())
        try:
            general = json.loads(rows.get("general", "{}"))
            if isinstance(general, dict):
                settings.update({k: v for k, v in general.items() if k in settings})
        except (ValueError, TypeError):
            pass
        for key in settings:
            if key in rows:
                try:
                    settings[key] = json.loads(rows[key])
                except (ValueError, TypeError):
                    settings[key] = rows[key]
    except aiosqlite.OperationalError:
        # The settings table need not exist in older installations.
        pass
    try:
        value = settings["concurrency_limit"]
        limit = int(value)
        if isinstance(value, bool) or limit < 1 or float(value) != limit:
            raise ValueError("invalid concurrency")
        settings["concurrency_limit"] = limit
    except (TypeError, ValueError, OverflowError):
        settings["concurrency_limit"] = 1
    return settings


async def _append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "a", encoding="utf-8") as file:
        for frame in line.splitlines() or [""]:
            await file.write(f"[{datetime.now():%H:%M:%S}] {frame}\n")
        await file.flush()


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Always reap, including a child that exits between the check and signal."""
    async def drain(stream: asyncio.StreamReader) -> None:
        while await stream.read(4096):
            pass

    # Cancelled log readers may have left full pipes. Keep draining while
    # waiting: Process.wait() can otherwise hang even after the child is killed.
    drains = [asyncio.create_task(drain(stream))
              for stream in (proc.stdout, proc.stderr) if stream is not None]
    try:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), TERMINATE_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        await proc.wait()
    finally:
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


class TaskRunner:
    """Fixed workers own executions; lock also protects router deletion checks.

    Routers may hold ``lock`` to check ``is_active`` and delete a non-running
    row/files atomically. Do not call start/enqueue/cancel_task while holding it.
    """

    _instance: TaskRunner | None = None

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._executions: dict[str, asyncio.Task] = {}
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._queued: set[str] = set()
        self._is_started = False
        self._db_path: Path | None = None
        self._concurrency_limit = 1

    @classmethod
    def get_instance(cls) -> TaskRunner:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def is_active(self, task_id: str) -> bool:
        """True until execution cleanup finishes; queued IDs are not active."""
        return task_id in self._executions

    async def fetch_task(self, task_id: str) -> dict | None:
        return await _fetch_task(task_id, self._db_path or Path(config.DB_PATH))

    async def start(self) -> None:
        async with self._lifecycle_lock:
            async with self.lock:
                if self._is_started:
                    return
                self._db_path = Path(config.DB_PATH).resolve()
                settings = await read_general_settings(self._db_path)
                self._concurrency_limit = settings["concurrency_limit"]
                # Recover before workers start; one shared dedup set covers recovery
                # and an enqueue that itself triggered startup.
                async with aiosqlite.connect(self._db_path) as db:
                    await db.execute(
                        "UPDATE tasks SET status = 'failed', error_message = ?, "
                        "updated_at = ? WHERE status = 'running'",
                        ("Task interrupted by server shutdown/restart", _now()),
                    )
                    async with db.execute(
                        "SELECT id FROM tasks WHERE status = 'pending' ORDER BY created_at, id"
                    ) as cursor:
                        pending = await cursor.fetchall()
                    await db.commit()
                self.queue = asyncio.Queue()
                self._queued.clear()
                for (task_id,) in pending:
                    self._put(task_id)
                self._is_started = True
                self._workers = [
                    asyncio.create_task(self._worker_loop(), name=f"book-worker-{i}")
                    for i in range(self._concurrency_limit)
                ]

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            async with self.lock:
                self._is_started = False
                workers = list(self._workers)
                for worker in workers:
                    worker.cancel()
            # Workers cancel and await their owned execution, which reaps its child
            # and flushes its final log before writing the terminal state.
            await asyncio.gather(*workers, return_exceptions=True)
            async with self.lock:
                self._workers.clear()
                self._queued.clear()
                while not self.queue.empty():
                    self.queue.get_nowait()
                    self.queue.task_done()
                self._db_path = None

    def _put(self, task_id: str) -> None:
        if task_id not in self._queued and not self.is_active(task_id):
            self._queued.add(task_id)
            self.queue.put_nowait(task_id)

    async def enqueue(self, task_id: str) -> None:
        await self.start()
        async with self.lock:
            if not self._is_started:
                raise RuntimeError("Task runner is stopping")
            task = await self.fetch_task(task_id)
            if task and task["status"] == "pending":
                self._put(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        async with self.lock:
            task = await self.fetch_task(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["status"] not in ("pending", "running", "cancelled"):
                raise ValueError(f"Task {task_id} is already {task['status']}")
            execution = self._executions.get(task_id)
            if task["status"] == "cancelled" and execution is None:
                return True
            if execution is not None:
                if not execution.cancelling():
                    execution.cancel()
            else:
                await _append_log(Path(task["log_path"]), "[System] Task cancelled.")
                async with aiosqlite.connect(self._db_path or config.DB_PATH) as db:
                    await db.execute(
                        "UPDATE tasks SET status = 'cancelled', updated_at = ? "
                        "WHERE id = ? AND status IN ('pending', 'running')",
                        (_now(), task_id),
                    )
                    await db.commit()
        if execution is not None:
            # Shield prevents a disconnected HTTP request from interrupting cleanup.
            await asyncio.shield(asyncio.gather(execution, return_exceptions=True))
            async with self.lock:
                if self._executions.get(task_id) is execution:
                    self._executions.pop(task_id, None)
        return True

    async def _worker_loop(self) -> None:
        while True:
            task_id = await self.queue.get()
            execution = None
            try:
                async with self.lock:
                    self._queued.discard(task_id)
                    if not self._is_started:
                        continue
                    task = await self.fetch_task(task_id)
                    if not task or task["status"] != "pending" or self.is_active(task_id):
                        continue
                    # The execution is registered before its first await. It owns
                    # the pending->running claim as well, so even immediate cancel
                    # cannot leave a running row without an execution.
                    execution = asyncio.create_task(self._execute_task(task))
                    self._executions[task_id] = execution
                    # Enter its try/finally before cancellation can acquire lock.
                    await asyncio.sleep(0)
                await asyncio.shield(execution)
            except asyncio.CancelledError:
                if execution is not None:
                    if not execution.cancelling():
                        execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                # An individually cancelled execution must not retire its worker.
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                logger.exception("Worker failed for task %s", task_id)
            finally:
                async with self.lock:
                    if self._executions.get(task_id) is execution:
                        self._executions.pop(task_id, None)
                self.queue.task_done()

    async def _execute_task(self, task: dict) -> None:
        task_id = task["id"]
        log_path = Path(task["log_path"])
        proc = None
        readers: list[asyncio.Task] = []
        stderr_tail: deque[str] = deque(maxlen=5)
        progress = (0, 0)
        io_lock = asyncio.Lock()
        status, error, output = "failed", None, None
        claimed = False
        try:
            async with self.lock:
                async with aiosqlite.connect(self._db_path) as db:
                    cursor = await db.execute(
                        "UPDATE tasks SET status = 'running', error_message = NULL, "
                        "updated_at = ? WHERE id = ? AND status = 'pending'",
                        (_now(), task_id),
                    )
                    claimed = cursor.rowcount == 1
                    await db.commit()
                if not claimed:
                    return
                source = Path(task["file_path"]).resolve()
                suffix = ".epub" if source.suffix.lower() == ".pdf" else source.suffix.lower()
                expected = source.with_name(f"{source.stem}_bilingual{suffix}")
                destination = Path(config.OUTPUT_DIR).resolve() / f"{task_id}_bilingual{suffix}"
                previous = expected.stat() if expected.exists() else None
                cmd = [sys.executable, "-m", "book_maker.cli", "--book_name", str(source),
                       "--language", task.get("target_language") or "zh-hans"]
                if task.get("model_provider"):
                    cmd.extend(["--provider", task["model_provider"]])
                if task.get("prompt_template"):
                    # Always JSON, never a server-local path interpreted by CLI.
                    cmd.extend(["--prompt", json.dumps({"user": task["prompt_template"]}, ensure_ascii=False)])
                env = os.environ.copy()
                for env_key, key in (await settings_service.get_provider_environment()).items():
                    if not env.get(env_key, "").strip():
                        env[env_key] = key
                env.update(PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
                env["PYTHONPATH"] = str(config.BASE_DIR.resolve()) + (
                    os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
                )
                config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                await _append_log(log_path, f"[System] Starting task {task_id}.")
                # Cancellation/deletion cannot interleave with spawning/registering.
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=str(config.CONFIG_DIR.resolve()), env=env,
                ))
                try:
                    proc = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    # A shutdown during process creation must still obtain/reap it.
                    proc = await spawn
                    raise
                self._active_processes[task_id] = proc

            async def frame(line: str, stderr: bool) -> None:
                nonlocal progress
                line = ANSI_RE.sub("", line).strip()
                if not line:
                    return
                async with io_lock:
                    await _append_log(log_path, ("[stderr] " if stderr else "") + line)
                    if stderr:
                        stderr_tail.append(line)
                    match = (PROGRESS_BRACKET_RE.search(line)
                             or PROGRESS_TRANSLATING_RE.search(line)
                             or PROGRESS_TQDM_RE.search(line))
                    if match:
                        try:
                            current, total = map(int, match.groups())
                        except ValueError:
                            return  # Ignore absurdly long numeric counters.
                        old_current, old_total = progress
                        if (0 <= current <= total <= 2**63 - 1 and total > 0
                                and current >= old_current and total >= old_total
                                and (not old_total or current * old_total >= old_current * total)
                                and (current, total) != progress):
                            await self._update_task_progress(task_id, current, total)
                            progress = current, total

            async def read_stream(stream: asyncio.StreamReader, stderr: bool) -> None:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                buffer = ""
                while True:
                    chunk = await stream.read(4096)
                    buffer += decoder.decode(chunk, final=not chunk)
                    while buffer:
                        separator = re.search(r"[\r\n]", buffer)
                        if separator and separator.start() < MAX_FRAME:
                            end = separator.start()
                            line, buffer = buffer[:end], buffer[end + 1:]
                        elif len(buffer) >= MAX_FRAME:
                            line, buffer = buffer[:MAX_FRAME], buffer[MAX_FRAME:]
                        elif not chunk:
                            line, buffer = buffer, ""
                        else:
                            break
                        await frame(line, stderr)
                    if not chunk:
                        break

            readers = [asyncio.create_task(read_stream(proc.stdout, False)),
                       asyncio.create_task(read_stream(proc.stderr, True))]
            await asyncio.gather(*readers)
            returncode = await proc.wait()
            if returncode != 0:
                raise RuntimeError(f"Process exited with code {returncode}" + (
                    ": " + "\n".join(stderr_tail) if stderr_tail else ""
                ))
            # Do not trust announcements of arbitrary paths, temp files, or an
            # old output left by a prior attempt that produced nothing this time.
            if not expected.is_file():
                raise RuntimeError("CLI exited successfully but no bilingual output was generated")
            actual = expected.stat()
            if previous and (actual.st_mtime_ns, actual.st_size, actual.st_ino) == (
                previous.st_mtime_ns, previous.st_size, previous.st_ino
            ):
                raise RuntimeError("CLI exited successfully but bilingual output was not updated")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if expected != destination:
                # Keep ownership until the move finishes (a cancelled thread-pool
                # move could otherwise outlive cancel_task and router deletion).
                shutil.move(str(expected), str(destination))
            output, status = str(destination), "completed"
        except asyncio.CancelledError:
            status, error = "cancelled", None
        except Exception as exc:
            error = str(exc)
            logger.warning("Task %s failed: %s", task_id, exc)
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if proc is not None:
                await _terminate(proc)
            async with self.lock:
                self._active_processes.pop(task_id, None)
                # Also covers cancellation before the pending claim's first await.
                if not claimed:
                    existing = await self.fetch_task(task_id)
                    if existing and existing["status"] == "pending" and status == "cancelled":
                        await _append_log(log_path, "[System] Task cancelled.")
                        async with aiosqlite.connect(self._db_path) as db:
                            await db.execute(
                                "UPDATE tasks SET status = 'cancelled', updated_at = ? "
                                "WHERE id = ? AND status = 'pending'", (_now(), task_id),
                            )
                            await db.commit()
                else:
                    await _append_log(log_path, f"[System] Task {status}." + (f" {error}" if error else ""))
                    current, total = progress
                    if status == "completed" and total:
                        current = total
                    async with aiosqlite.connect(self._db_path) as db:
                        await db.execute(
                            "UPDATE tasks SET status = ?, output_path = ?, error_message = ?, "
                            "progress_current = ?, progress_total = ?, updated_at = ? "
                            "WHERE id = ? AND status = 'running'",
                            (status, output, error, current, total, _now(), task_id),
                        )
                        await db.commit()

    async def _update_task_progress(self, task_id: str, current: int, total: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE tasks SET progress_current = ?, progress_total = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (current, total, _now(), task_id),
            )
            await db.commit()


runner = TaskRunner.get_instance()
