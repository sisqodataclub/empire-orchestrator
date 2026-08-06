# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.db.session import engine, Base
from app.routers import missions, dashboard, auth

# Create tables if they don't exist
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Empire Orchestrator - Multi-Tenant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router)                     # login/logout at root paths
app.include_router(dashboard.router, prefix="/api/v1")
app.include_router(missions.router, prefix="/api/v1")

@app.get("/health")
async def health():
    return {"status": "ok"}
