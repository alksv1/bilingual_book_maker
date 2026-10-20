"""Task API and offline subprocess lifecycle regression tests."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from server import config
from server.database import init_db
from server.main import app
from server.routers.tasks import runner
from server.services.runner import TaskRunner, fetch_task


@pytest.fixture
def storage(tmp_path, monkeypatch):
    for name, value in {
        "DATA_DIR": tmp_path, "UPLOADS_DIR": tmp_path / "uploads",
        "OUTPUT_DIR": tmp_path / "output", "LOGS_DIR": tmp_path / "logs",
        "CONFIG_DIR": tmp_path / "config", "DB_PATH": tmp_path / "tasks.db",
        "GENERAL_CONFIG_PATH": tmp_path / "config/general.json",
        "PROVIDERS_CONFIG_PATH": tmp_path / "config/bbm_providers.json",
    }.items():
        monkeypatch.setattr(config, name, value)
    return tmp_path


@pytest.fixture
def client(storage, monkeypatch):
    monkeypatch.setattr(runner, "enqueue", AsyncMock())
    with TestClient(app) as client:
        yield client


def upload(client, name="book.txt"):
    response = client.post("/api/tasks", files={"files": (name, b"hello world")})
    assert response.status_code == 201, response.text
    return response.json()[0]["id"]


def test_batch_atomic_validation(client):
    response = client.post("/api/tasks", files=[
        ("files", ("good.txt", b"hello")), ("files", ("bad.exe", b"bad")),
    ])
    assert response.status_code == 400
    assert client.get("/api/tasks").json()["total"] == 0
    assert not list(config.UPLOADS_DIR.iterdir())


def test_empty_upload_rolls_back(client):
    response = client.post("/api/tasks", files=[
        ("files", ("good.txt", b"hello")), ("files", ("empty.txt", b"")),
    ])
    assert response.status_code == 400
    assert client.get("/api/tasks").json()["total"] == 0
    assert not list(config.UPLOADS_DIR.iterdir())


def test_cancel_logs_delete_and_missing(client):
    task_id = upload(client, "../../book.txt")
    assert len(list(config.UPLOADS_DIR.iterdir())) == 1
    assert client.get("/api/tasks?status=invalid").status_code == 422
    assert client.get(f"/api/tasks/{task_id}/download").status_code == 404
    assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
    assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
    logs = client.get(f"/api/tasks/{task_id}/logs/stream")
    assert logs.status_code == 200
    assert "text/event-stream" in logs.headers["content-type"]
    assert client.get("/api/tasks?status=cancelled").json()["total"] == 1
    assert client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert not list(config.UPLOADS_DIR.iterdir())
    assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 404
    assert client.get(f"/api/tasks/{task_id}/logs/stream").status_code == 404


async def insert_task(task_id="task_test", status="pending"):
    source = config.UPLOADS_DIR / f"{task_id}.txt"
    source.write_text("hello", encoding="utf-8")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """INSERT INTO tasks (id, filename, file_path, file_type, status,
               output_path, log_path) VALUES (?, ?, ?, 'txt', ?, ?, ?)""",
            (task_id, "book.txt", str(source), status,
             str(config.OUTPUT_DIR / f"{task_id}_bilingual.txt"),
             str(config.LOGS_DIR / f"{task_id}.log")),
        )
        await db.commit()
    return source


async def wait_terminal(task_id):
    async def wait():
        while True:
            task = await fetch_task(task_id)
            if task["status"] not in ("pending", "running"):
                return task
            await asyncio.sleep(0.02)
    return await asyncio.wait_for(wait(), 10)


@pytest.mark.asyncio
async def test_real_child_progress_and_output(storage, monkeypatch):
    await init_db()
    source = await insert_task()
    real_spawn = asyncio.create_subprocess_exec
    commands = []

    async def spawn(*args, **kwargs):
        commands.append(args)
        program = (
            "import sys; from pathlib import Path; "
            "sys.stderr.write(' 50%|#####| 1/2 [00:01<00:01]\\r'); sys.stderr.flush(); "
            "print('chunk [2/2]'); "
            f"Path({str(source.with_name(source.stem + '_bilingual.txt'))!r}).write_text('translated'); "
            f"print({'Bilingual book saved: ' + str(source.with_name(source.stem + '_bilingual.txt'))!r})"
        )
        return await real_spawn(sys.executable, "-u", "-c", program, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    service = TaskRunner()
    await service.start()
    try:
        await service.enqueue("task_test")
        task = await wait_terminal("task_test")
        assert task["status"] == "completed", task
        assert (task["progress_current"], task["progress_total"]) == (2, 2)
        assert Path(task["output_path"]).read_text() == "translated"
        assert "--output_path" not in commands[0]
        assert "book_maker.cli" in commands[0]
        assert "chunk [2/2]" in Path(task["log_path"]).read_text()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_failure_and_recovery(storage, monkeypatch):
    await init_db()
    await insert_task("old", "running")
    await insert_task("task_test")
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        return await real_spawn(sys.executable, "-c", "import sys; print('failure', file=sys.stderr); sys.exit(7)", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    service = TaskRunner()
    await service.start()
    try:
        assert (await fetch_task("old"))["status"] == "failed"
        task = await wait_terminal("task_test")
        assert task["status"] == "failed"
        assert "failure" in task["error_message"]
        with pytest.raises(ValueError):
            await service.cancel_task("task_test")
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_cancel_real_running_process(storage, monkeypatch):
    await init_db()
    await insert_task()
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await real_spawn(sys.executable, "-u", "-c", "import time; print('started'); time.sleep(60)", **kwargs)
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    service = TaskRunner()
    await service.start()
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        await service.cancel_task("task_test")
        assert (await fetch_task("task_test"))["status"] == "cancelled"
        assert processes[0].returncode is not None
        assert not service.is_active("task_test")
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_zero_exit_without_output_fails(storage, monkeypatch):
    await init_db()
    await insert_task()
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        return await real_spawn(sys.executable, "-c", "print('nothing produced')", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    service = TaskRunner()
    await service.start()
    try:
        task = await wait_terminal("task_test")
        assert task["status"] == "failed"
        assert "output" in task["error_message"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_concurrency_dedup_and_shutdown(storage, monkeypatch):
    await init_db()
    await insert_task("first")
    await insert_task("second")
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await real_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    service = TaskRunner()
    await service.start()
    try:
        await service.enqueue("first")
        await asyncio.wait_for(spawned.wait(), 5)
        assert len(processes) == 1
        assert (await fetch_task("second"))["status"] == "pending"
        await service.cancel_task("second")
        await service.stop()
        assert len(processes) == 1
        assert processes[0].returncode is not None
        assert (await fetch_task("first"))["status"] == "cancelled"
        assert (await fetch_task("second"))["status"] == "cancelled"
    finally:
        await service.stop()


def test_download_and_terminal_cancel(client):
    import sqlite3
    task_id = upload(client)
    output = config.OUTPUT_DIR / f"{task_id}_bilingual.txt"
    output.write_text("translated", encoding="utf-8")
    with sqlite3.connect(config.DB_PATH) as db:
        db.execute("UPDATE tasks SET status='completed', output_path=? WHERE id=?", (str(output), task_id))
    response = client.get(f"/api/tasks/{task_id}/download")
    assert response.status_code == 200
    assert response.text == "translated"
    assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 409
    assert client.delete(f"/api/tasks/{task_id}?delete_file=false").status_code == 204
    assert output.exists()



def test_sse_history_and_default_settings(client):
    import json
    import sqlite3
    config.GENERAL_CONFIG_PATH.write_text(json.dumps({
        "default_target_lang": "ja", "active_provider": "local-test"
    }), encoding="utf-8")
    task_id = upload(client)
    task = client.get("/api/tasks").json()["items"][0]
    assert task["target_language"] == "ja"
    assert task["model_provider"] == "local-test"
    log = config.LOGS_DIR / f"{task_id}.log"
    log.write_text('[12:00:00] first' + chr(10) + '[12:00:01] last' + chr(10), encoding="utf-8")
    with sqlite3.connect(config.DB_PATH) as db:
        db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
    response = client.get(f"/api/tasks/{task_id}/logs/stream")
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert frames == [{"time": "12:00:00", "line": "first"}, {"time": "12:00:01", "line": "last"}]


@pytest.mark.asyncio
async def test_sse_live_tail_drains_final_log(storage):
    from server.routers.tasks import stream_task_logs
    await init_db()
    await insert_task()
    response = await stream_task_logs("task_test")
    stream = response.body_iterator
    assert await anext(stream) == ": keepalive" + chr(10) * 2
    log = config.LOGS_DIR / "task_test.log"
    log.write_text("[12:00:00] final" + chr(10), encoding="utf-8")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE tasks SET status='completed' WHERE id='task_test'")
        await db.commit()
    event = await anext(stream)
    assert '"line": "final"' in event
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
