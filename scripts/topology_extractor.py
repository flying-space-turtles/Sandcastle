#!/usr/bin/env python3
"""
Standalone script to extract and display topology from docker-compose files.
Can be run independently to generate sample topology JSON.
"""

import yaml
import json
from pathlib import Path
from typing import Dict, List, Any

class StandaloneTopologyExtractor:
    def __init__(self, repo_root: Path = None):
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = repo_root
        self.compose_file = repo_root / "docker-compose.yml"

    def extract(self) -> Dict[str, Any]:
        """Extract topology from static docker-compose file."""
        if not self.compose_file.exists():
            print(f"Warning: docker-compose.yml not found at {self.compose_file}")
            return {"nodes": [], "edges": [], "teams": []}
        
        with open(self.compose_file, 'r') as f:
            compose = yaml.safe_load(f)
        
        nodes = []
        edges = []
        teams = set()
        
        services = compose.get('services', {})
        
        # Create nodes for each service
        for service_name, service_config in services.items():
            if 'ssh' in service_name:
                node_type = 'gateway'
                x_pos = 0
                color = '#06b6d4'  # cyan
            elif 'vuln' in service_name:
                node_type = 'service'
                x_pos = 300
                color = '#8b5cf6'  # purple
            else:
                node_type = 'service'
                x_pos = 600
                color = '#ef4444'  # red
            
            # Extract team
            team_match = service_name.split('-')[0]
            teams.add(team_match)
            
            # Get IP
            networks = service_config.get('networks', {})
            ip_address = 'N/A'
            if isinstance(networks, dict) and 'ctf-network' in networks:
                ip_address = networks['ctf-network'].get('ipv4_address', 'N/A')
            
            # Calculate y position
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
                    "color": color
                },
                "position": {"x": x_pos, "y": y_pos},
                "type": node_type
            }
            nodes.append(node)
        
        # Create edges
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

if __name__ == "__main__":
    import sys
    
    extractor = StandaloneTopologyExtractor()
    topology = extractor.extract()
    
    # Output to stdout or file
    if len(sys.argv) > 1:
        output_file = Path(sys.argv[1])
        with open(output_file, 'w') as f:
            json.dump(topology, f, indent=2)
        print(f"Topology written to {output_file}")
    else:
        print(json.dumps(topology, indent=2))
