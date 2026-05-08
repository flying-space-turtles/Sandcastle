from pydantic import BaseModel
from typing import List, Dict, Any, Optional

class Node(BaseModel):
    id: str
    data: Dict[str, Any]
    position: Dict[str, float]
    type: str  # 'gateway', 'service', 'subnet'

class Edge(BaseModel):
    id: str
    source: str
    target: str
    type: Optional[str] = "default"

class Team(BaseModel):
    id: str
    name: str

class TopologyResponse(BaseModel):
    nodes: List[Node]
    edges: List[Edge]
    teams: List[Team]

class Settings(BaseModel):
    live_mode: bool = True
