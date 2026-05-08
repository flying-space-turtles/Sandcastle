import os
import yaml
import json
from pathlib import Path
from typing import Dict, List, Any, Tuple
import docker
import asyncio
import logging
try:
    import requests_unixsocket
except Exception:
    requests_unixsocket = None

log = logging.getLogger("topology_extractor")
logging.basicConfig(level=logging.INFO)

class TopologyExtractor:
    def __init__(self):
        # Allow overriding repo root (useful when running in container)
        env_root = os.environ.get("REPO_ROOT")
        if env_root:
            self.repo_root = Path(env_root).resolve()
        else:
            # default: three levels up from this file (repo root in dev layout)
            self.repo_root = Path(__file__).resolve().parent.parent.parent

        self.compose_file = self.repo_root / "docker-compose.yml"
        log.info(f"TopologyExtractor repo_root={self.repo_root}")
        log.info(f"Looking for compose file at: {self.compose_file}")
        log.info(f"DOCKER_HOST env: {os.environ.get('DOCKER_HOST')}")
        self.docker_client = None
        try:
            self.docker_client = docker.from_env()
        except Exception as e:
            log.warning(f"Could not connect to Docker via from_env(): {e}")
            # Try a unix-socket direct client as a fallback
            try:
                self.docker_client = docker.DockerClient(base_url="unix://var/run/docker.sock")
                log.info("Connected to Docker via unix socket fallback")
            except Exception as e2:
                log.warning(f"Unix socket fallback failed: {e2}")

    def extract_static(self) -> Dict[str, Any]:
        """Extract topology from static docker-compose files."""
        if not self.compose_file.exists():
            return {"nodes": [], "edges": [], "teams": []}
            
        with open(self.compose_file, 'r') as f:
            compose = yaml.safe_load(f)
        
        nodes = []
        edges = []
        teams = set()
        
        services = compose.get('services', {})
        
        # Extract teams and create nodes
        for service_name, service_config in services.items():
            # Determine service type and position
            if 'ssh' in service_name:
                node_type = 'gateway'
                x_pos = 0
            elif 'vuln' in service_name:
                node_type = 'service'
                x_pos = 300
            else:
                node_type = 'service'
                x_pos = 600
            
            # Extract team from service name
            team_match = service_name.split('-')[0]  # e.g., "team1" from "team1-vuln"
            teams.add(team_match)
            
            # Get IP address if available
            networks = service_config.get('networks', {})
            ip_address = None
            if isinstance(networks, dict) and 'ctf-network' in networks:
                ip_address = networks['ctf-network'].get('ipv4_address', 'N/A')
            
            # Get environment info
            env = service_config.get('environment', {})
            
            # Calculate positions (grid layout by team and type)
            team_num = int(''.join(filter(str.isdigit, team_match))) if any(c.isdigit() for c in team_match) else 0
            y_pos = team_num * 200
            
            node = {
                "id": service_name,
                "data": {
                    "label": service_name,
                    "type": node_type,
                    "team": team_match,
                    "ip": ip_address,
                    "image": service_config.get('image', 'N/A'),
                },
                "position": {"x": x_pos, "y": y_pos},
                "type": node_type
            }
            nodes.append(node)
        
        # Create edges for team connections
        for team in teams:
            ssh_service = f"{team}-ssh"
            vuln_service = f"{team}-vuln"
            
            ssh_exists = any(n["id"] == ssh_service for n in nodes)
            vuln_exists = any(n["id"] == vuln_service for n in nodes)
            
            if ssh_exists and vuln_exists:
                edge = {
                    "id": f"{ssh_service}-{vuln_service}",
                    "source": ssh_service,
                    "target": vuln_service,
                    "type": "smoothstep"
                }
                edges.append(edge)
        
        teams_list = [{"id": t, "name": t.replace('-', ' ').title()} for t in sorted(teams)]
        
        return {
            "nodes": nodes,
            "edges": edges,
            "teams": teams_list
        }

    async def extract_live(self) -> Dict[str, Any]:
        """Extract topology from live Docker containers."""
        topology = self.extract_static()

        # If docker SDK is available, use it
        if self.docker_client is not None:
            try:
                containers = self.docker_client.containers.list(all=True)
                for container in containers:
                    container_name = container.name
                    for node in topology["nodes"]:
                        if node["id"] == container_name:
                            node["data"]["status"] = container.status
                            node["data"]["container_id"] = container.short_id
                            ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
                            if ports:
                                node["data"]["ports"] = ports
                            try:
                                stats = container.stats(stream=False)
                                memory_bytes = stats.get('memory_stats', {}).get('usage', 0)
                                node["data"]["memory_usage"] = f"{memory_bytes / (1024**2):.2f} MB"
                            except Exception:
                                pass
                return topology
            except Exception as e:
                log.warning(f"Error using docker SDK for live extraction: {e}")

        # Fallback: use requests_unixsocket to query Docker API directly
        if requests_unixsocket is None:
            log.warning("requests_unixsocket not available; returning static topology")
            return topology

        try:
            session = requests_unixsocket.Session()
            resp = session.get('http+docker://localhost/containers/json?all=1')
            resp.raise_for_status()
            containers = resp.json()
            for c in containers:
                # c has Id, Names (list), State, Status
                names = c.get('Names') or []
                # Names are like ['/container_name']
                name = names[0].lstrip('/') if names else c.get('Names')
                for node in topology['nodes']:
                    if node['id'] == name:
                        node['data']['status'] = c.get('State') or c.get('Status')
                        node['data']['container_id'] = c.get('Id')[:12]
                        # inspect for more details
                        try:
                            insp = session.get(f"http+docker://localhost/containers/{c.get('Id')}/json")
                            insp.raise_for_status()
                            info = insp.json()
                            net = info.get('NetworkSettings', {})
                            ports = net.get('Ports', {})
                            if ports:
                                node['data']['ports'] = ports
                        except Exception as ie:
                            log.warning(f"Failed to inspect container {c.get('Id')}: {ie}")
            return topology
        except Exception as e:
            log.warning(f"Error extracting live topology via socket: {e}")
            return topology
