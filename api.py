# api.py
import json, time, threading
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List

from orchestration.task_manager import TaskManager

app = FastAPI()
manager = TaskManager()

class MissionRequest(BaseModel):
    mission: str
    agents: List[dict]

@app.post("/missions/start")
async def start_mission(req: MissionRequest):
    # Quick helper: convert dict to NativeAgent (reuse your gm.py logic)
    from gm import NativeAgent, get_population  # or import your create_agent
    agents = [NativeAgent(**a) for a in req.agents]
    task_id = manager.start_mission(req.mission, agents)
    return {"task_id": task_id}

@app.get("/missions/{task_id}/stream")
async def stream(task_id: str):
    task = manager.get_task(task_id)
    if not task: raise HTTPException(404)

    def event_generator():
        last = 0
        while not task.is_complete:
            while last < len(task.logs):
                yield f"data: {json.dumps(task.logs[last])}\n\n"
                last += 1
            time.sleep(0.5)
        yield f"data: {json.dumps({'status':'COMPLETE','result':task.result})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
