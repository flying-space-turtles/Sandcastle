import { useCallback, useMemo, useState } from 'react';
import defaultComposeYaml from '../../docker-compose.yml?raw';
import sshDockerfile from '../../docker/ssh/Dockerfile?raw';
import vulnMachineDockerfile from '../../docker/vuln/Dockerfile?raw';
import firewallDockerfile from '../../firewall/Dockerfile?raw';
import vulnDockerfile from '../../services/example-vuln/Dockerfile?raw';
import AgentsPanel from './components/AgentsPanel';
import BotPanel from './components/BotPanel';
import DetailsPanel from './components/DetailsPanel';
import DockerCanvas from './components/DockerCanvas';
import EventFeed from './components/EventFeed';
import Scoreboard from './components/Scoreboard';
import TopologyNav from './components/TopologyNav';
import { parseDockerCompose } from './data/dockerComposeParser';
import { buildDockerFlow } from './graph/dockerGraph';
import { useNetworkEvents } from './hooks/useNetworkEvents';
import type { MachineNodeData, Mode, Topology } from './types';

const dockerfileSources: Record<string, string> = {
  'ssh/Dockerfile': sshDockerfile,
  './ssh/Dockerfile': sshDockerfile,
  'docker/ssh/Dockerfile': sshDockerfile,
  './docker/ssh/Dockerfile': sshDockerfile,
  'docker/vuln/Dockerfile': vulnMachineDockerfile,
  './docker/vuln/Dockerfile': vulnMachineDockerfile,
  'firewall/Dockerfile': firewallDockerfile,
  './firewall/Dockerfile': firewallDockerfile,
  'services/example-vuln/Dockerfile': vulnDockerfile,
  './services/example-vuln/Dockerfile': vulnDockerfile,
  'teams/generated/team1/example-vuln/Dockerfile': vulnDockerfile,
  './teams/generated/team1/example-vuln/Dockerfile': vulnDockerfile,
  'teams/generated/team2/example-vuln/Dockerfile': vulnDockerfile,
  './teams/generated/team2/example-vuln/Dockerfile': vulnDockerfile,
  'teams/generated/team3/example-vuln/Dockerfile': vulnDockerfile,
  './teams/generated/team3/example-vuln/Dockerfile': vulnDockerfile,
  'teams/generated/team4/example-vuln/Dockerfile': vulnDockerfile,
  './teams/generated/team4/example-vuln/Dockerfile': vulnDockerfile,
  'teams/generated/team5/example-vuln/Dockerfile': vulnDockerfile,
  './teams/generated/team5/example-vuln/Dockerfile': vulnDockerfile,
  'teams/generated/team6/example-vuln/Dockerfile': vulnDockerfile,
  './teams/generated/team6/example-vuln/Dockerfile': vulnDockerfile,
  Dockerfile: vulnDockerfile,
  './Dockerfile': vulnDockerfile,
};

const buildTopology = (yamlSource: string): Topology => {
  const parsedCompose = parseDockerCompose(yamlSource, dockerfileSources);
  const parsed = {
    ...parsedCompose,
    services: parsedCompose.services.filter(
      (service) => service.labels['sandcastle.visualizer.hidden'] !== 'true',
    ),
  };
  const flow = buildDockerFlow(parsed);

  return {
    parsed,
    ...flow,
  };
};

const App = () => {
  const [mode, setMode] = useState<Mode>('scoreboard');
  const topology = useMemo(() => buildTopology(defaultComposeYaml), []);
  const [selectedNode, setSelectedNode] = useState<MachineNodeData | null>(null);

  const { events, connected, liveEdges } = useNetworkEvents();

  const summary = useMemo(
    () => ({
      serviceCount: topology.parsed.services.length,
      networkCount: topology.parsed.networks.length || 1,
      edgeCount: topology.edges.length,
    }),
    [topology],
  );

  const handleSelectNode = useCallback((node: MachineNodeData | null) => {
    setSelectedNode(node);
  }, []);

  return (
    <div className="app-shell ops-shell">
      <TopologyNav
        mode={mode}
        onModeChange={setMode}
        serviceCount={summary.serviceCount}
        networkCount={summary.networkCount}
        edgeCount={summary.edgeCount}
        firewallConnected={connected}
        firewallEventCount={events.length}
      />

      <div className="ops-content">
      {mode === 'scoreboard' && <Scoreboard />}

      {mode === 'topology' && (
        <main className="workspace workspace--canvas">
          <section className="canvas-shell">
            <div className="topology-context">
              Configured topology <span>Not live container health</span>
            </div>
            <DockerCanvas topology={topology} onSelectNode={handleSelectNode} liveEdges={liveEdges} />
          </section>
          <DetailsPanel node={selectedNode} />
        </main>
      )}

      {mode === 'firewall' && (
        <main className="workspace workspace--canvas">
          <section className="canvas-shell">
            <div className="topology-context">
              Live traffic <span>Nodes come from configured Compose</span>
            </div>
            <DockerCanvas topology={topology} onSelectNode={handleSelectNode} liveEdges={liveEdges} />
          </section>
          <EventFeed events={events} connected={connected} />
        </main>
      )}

      {mode === 'bot' && <BotPanel />}
      {mode === 'agents' && <AgentsPanel />}
      </div>
    </div>
  );
};

export default App;
