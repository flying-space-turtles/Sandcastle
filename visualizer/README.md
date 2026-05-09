# Sandcastle Docker Visualizer

React + React Flow module for rendering the Sandcastle Docker topology.

## Run

```bash
cd visualizer
npm install
npm run dev
```

The app loads the repository root `docker-compose.yml` by default. Use
`Yaml Mode` to paste or upload another Compose file, then render it into the
diagram.

## Data Model

The parser normalizes Compose metadata into React Flow nodes and edges:

- services become machine nodes with team, IP, environment, label, port, and
  Dockerfile metadata where available
- Compose networks become colored group nodes
- network IPAM gateways become synthetic gateway nodes
- `depends_on`, `links`, team gateway access, and network membership become
  labeled edges
