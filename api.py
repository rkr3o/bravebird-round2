"""
REST API wrapper around the bot runner.

Endpoints:
  POST /run         — trigger a bot run with optional input overrides
  GET  /status      — last run status and manifest
  GET  /download/{file} — download an output file
"""

import asyncio
import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="GitHub Automation Bot API")

OUTPUT_DIR = Path(__file__).parent / "output"
INPUT_PATH = Path(__file__).parent / "input.json"

# track the current run
_run_lock = threading.Lock()
_running = False


class RunRequest(BaseModel):
    repo: str = None
    issue_title: str = None
    issue_body: str = None
    labels: list[str] = None
    download_release: str = None


@app.post("/run")
async def trigger_run(request: RunRequest = None):
    global _running

    if not _run_lock.acquire(blocking=False):
        return JSONResponse({"error": "A run is already in progress"}, status_code=409)

    try:
        _running = True

        # apply overrides to input.json if provided
        with open(INPUT_PATH) as f:
            inputs = json.load(f)

        if request:
            if request.repo:
                inputs["repo"] = request.repo
            if request.issue_title:
                inputs["issue"]["title"] = request.issue_title
            if request.issue_body:
                inputs["issue"]["body"] = request.issue_body
            if request.labels is not None:
                inputs["issue"]["labels"] = request.labels
            if request.download_release:
                inputs["download_release"] = request.download_release

        # write the updated inputs
        with open(INPUT_PATH, "w") as f:
            json.dump(inputs, f, indent=2)

        # run the bot
        from bot import run
        exit_code = await run()

        # return the manifest
        manifest_path = OUTPUT_DIR / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            return JSONResponse(manifest)

        return JSONResponse({"status": "failed", "exit_code": exit_code})

    finally:
        _running = False
        _run_lock.release()


@app.get("/status")
async def get_status():
    if _running:
        return JSONResponse({"status": "running"})

    manifest_path = OUTPUT_DIR / "manifest.json"
    if not manifest_path.exists():
        return JSONResponse({"status": "no runs yet"})

    return JSONResponse(json.loads(manifest_path.read_text()))


@app.get("/download/{filename}")
async def download_file(filename: str):
    allowed_dirs = [OUTPUT_DIR / "downloads", OUTPUT_DIR / "evidence"]

    for directory in allowed_dirs:
        path = directory / filename
        if path.exists():
            return FileResponse(path, filename=filename)

    # also check output root
    path = OUTPUT_DIR / filename
    if path.exists():
        return FileResponse(path, filename=filename)

    raise HTTPException(404, f"File not found: {filename}")
