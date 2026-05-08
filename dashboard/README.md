# Sandcastle Topology Visualizer

A React + FastAPI web application that visualizes the Sandcastle CTF infrastructure as an interactive network diagram using React Flow.

## Features

- 🔄 **Live Mode** (default): Real-time Docker container state via Docker socket
- 📊 **Static Mode**: Parse topology from docker-compose files
- 🎨 **Interactive Canvas**: Pan, zoom, and click nodes for details
- ⚙️ **Settings Panel**: Toggle between live and static modes
- 🔐 **Team-based Layout**: Organized visualization of team gateways and services
- 🌐 **Network View**: See inter-container connections and network assignments

## Project Structure

```
dashboard/
├── src/
│   ├── components/          # React components
│   ├── App.tsx             # Main app component
│   ├── main.tsx            # Entry point
│   └── index.css           # Tailwind styles
├── api/
│   ├── main.py             # FastAPI server
│   ├── models.py           # Pydantic models
│   ├── topology_extractor.py  # Extraction logic
│   └── requirements.txt
├── public/                 # Static assets
├── package.json
├── vite.config.ts
└── index.html
```

## Setup

### Prerequisites

- Node.js 16+
- Python 3.9+
- Docker (for live mode)

### Installation

```bash
# Install frontend dependencies
cd dashboard
npm install

# Create and activate Python virtual environment
python3 -m venv venv
source venv/bin/activate  # macOS/Linux
# or: venv\Scripts\activate  # Windows

# Install backend dependencies
pip install -r api/requirements.txt
```

## Running

**Terminal 1 - Frontend (Vite dev server):**
```bash
cd dashboard
npm run dev
# Opens at http://localhost:5173
```

**Terminal 2 - Backend (FastAPI server):**
```bash
cd dashboard
source venv/bin/activate
python api/main.py
# Runs at http://localhost:8000
```

The frontend proxy automatically forwards `/api/*` requests to the backend.

## Usage

1. Open http://localhost:5173 in your browser
2. The topology will load automatically (live mode by default)
3. **Live Mode**: Shows real-time Docker container states
4. **Static Mode**: Shows topology from docker-compose.yml
5. **Click nodes** to view detailed information
6. **Drag to pan**, **scroll to zoom**

## API Endpoints

- `GET /health` - Health check
- `GET /topology?live=true` - Get topology (live or static)
- `GET /settings` - Get current settings
- `POST /settings` - Update settings (`{"live_mode": boolean}`)

## Topology Data Model

### Node
```typescript
{
  id: string,
  data: {
    label: string,
    type: "gateway" | "service",
    team: string,
    ip: string,
    image: string,
    status?: string,
    container_id?: string,
    ports?: {},
    memory_usage?: string
  },
  position: { x: number, y: number },
  type: string
}
```

### Edge
```typescript
{
  id: string,
  source: string,
  target: string,
  type: string
}
```

## Generating Sample Topology

To generate topology from current docker-compose.yml:

```bash
python scripts/topology_extractor.py dashboard/public/topology.sample.json
```

## Development

- Frontend: TypeScript + React + React Flow + Tailwind CSS
- Backend: Python FastAPI with Pydantic validation
- Real-time data: Docker Python SDK for live container introspection
- Static parsing: YAML parsing of compose files

## Troubleshooting

**Backend won't connect to Docker:**
- Live mode will gracefully fall back to static mode
- Ensure Docker daemon is running and accessible

**CORS errors:**
- Check that both servers are running on expected ports
- Frontend expects backend at `http://localhost:8000`

**No topology data:**
- Verify docker-compose.yml exists in repository root
- Check that services follow naming convention (team{N}-ssh, team{N}-vuln)

## Docker / Containerized Backend

You can run the FastAPI backend in a container using the provided compose file.

Build and run the backend with Docker Compose (from `dashboard/`):

```bash
cd dashboard
docker compose up --build -d topology-api
```

The API will be reachable at `http://localhost:8000`.

To stop and remove the container:

```bash
docker compose down
```


## Future Enhancements

- Service icons and custom node rendering
- Network subnet visualization
- Export as PNG/SVG
- Real-time port status indicators
- Container resource gauges (CPU, memory)
- Historical state tracking
