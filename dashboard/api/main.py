from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import json
from pathlib import Path
from topology_extractor import TopologyExtractor
from models import TopologyResponse, Settings

# Settings store
settings = Settings(live_mode=True)
extractor = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global extractor
    extractor = TopologyExtractor()
    yield

app = FastAPI(
    title="Sandcastle Topology API",
    description="API for generating Docker network topology from compose files",
    version="0.1.0",
    lifespan=lifespan
)

# CORS configuration for Vite dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/topology")
async def get_topology(live: bool = True):
    """
    Fetch the Docker network topology.
    
    - **live**: If True, fetch live container states from Docker. Otherwise use static config.
    """
    if extractor is None:
        return {"error": "Extractor not initialized"}
    
    try:
        if settings.live_mode and live:
            topology = await extractor.extract_live()
        else:
            topology = extractor.extract_static()
        
        return TopologyResponse(
            nodes=topology["nodes"],
            edges=topology["edges"],
            teams=topology["teams"]
        )
    except Exception as e:
        return {"error": str(e)}

@app.post("/settings")
async def update_settings(body: dict):
    """Update API settings."""
    if "live_mode" in body:
        settings.live_mode = body["live_mode"]
    return {"status": "ok", "settings": settings.model_dump()}

@app.get("/settings")
async def get_settings():
    """Get current settings."""
    return settings.model_dump()

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
