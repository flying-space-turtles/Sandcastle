import { useCallback, useMemo, useState, type ChangeEvent } from 'react';
import defaultComposeYaml from '../../docker-compose.yml?raw';
import exampleComposeYaml from '../../services/example-vuln/docker-compose.yml?raw';
import sshDockerfile from '../../docker/ssh/Dockerfile?raw';
import vulnMachineDockerfile from '../../docker/vuln/Dockerfile?raw';
import firewallDockerfile from '../../firewall/Dockerfile?raw';
import vulnDockerfile from '../../services/example-vuln/Dockerfile?raw';
import DetailsPanel from './components/DetailsPanel';
import DockerCanvas from './components/DockerCanvas';
import EventFeed from './components/EventFeed';
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
  const parsed = parseDockerCompose(yamlSource, dockerfileSources);
  const flow = buildDockerFlow(parsed);

  return {
    parsed,
    ...flow,
  };
};

const App = () => {
  const [mode, setMode] = useState<Mode>('editor');
  const [draftYaml, setDraftYaml] = useState<string>(defaultComposeYaml);
  const [topology, setTopology] = useState<Topology>(() => buildTopology(defaultComposeYaml));
  const [parseError, setParseError] = useState<string | null>(null);
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

  const applyYaml = useCallback(
    (yamlSource: string = draftYaml) => {
      try {
        const nextTopology = buildTopology(yamlSource);
        setTopology(nextTopology);
        setDraftYaml(yamlSource);
        setParseError(null);
        setSelectedNode(null);
        setMode('editor');
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Failed to parse YAML.';
        setParseError(message);
        setMode('yaml');
      }
    },
    [draftYaml],
  );

  const handlePreset = (yamlSource: string) => {
    setDraftYaml(yamlSource);
    applyYaml(yamlSource);
  };

  const handleFileUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    if (!file) {
      return;
    }

    const text = await file.text();
    setDraftYaml(text);
    applyYaml(text);
    event.currentTarget.value = '';
  };

  const handleSelectNode = useCallback((node: MachineNodeData | null) => {
    setSelectedNode(node);
  }, []);

  return (
    <div className="app-shell">
      <TopologyNav
        mode={mode}
        onModeChange={setMode}
        parseError={parseError}
        serviceCount={summary.serviceCount}
        networkCount={summary.networkCount}
        edgeCount={summary.edgeCount}
        firewallConnected={connected}
        firewallEventCount={events.length}
      />

      {mode === 'editor' && (
        <main className="workspace workspace--canvas">
          <section className="canvas-shell">
            <DockerCanvas topology={topology} onSelectNode={handleSelectNode} liveEdges={liveEdges} />
          </section>
          <DetailsPanel node={selectedNode} />
        </main>
      )}

      {mode === 'firewall' && (
        <main className="workspace workspace--canvas">
          <section className="canvas-shell">
            <DockerCanvas topology={topology} onSelectNode={handleSelectNode} liveEdges={liveEdges} />
          </section>
          <EventFeed events={events} connected={connected} />
        </main>
      )}

      {mode === 'yaml' && (
        <main className="workspace workspace--yaml">
          <section className="yaml-editor">
            <div className="yaml-editor__toolbar">
              <div>
                <h1>Compose Source</h1>
                <p>Paste a Docker Compose file or load one from disk.</p>
              </div>
              <div className="yaml-editor__actions">
                <button type="button" onClick={() => handlePreset(defaultComposeYaml)}>
                  Repository Compose
                </button>
                <button type="button" onClick={() => handlePreset(exampleComposeYaml)}>
                  Example Service
                </button>
                <label>
                  Upload YAML
                  <input type="file" accept=".yml,.yaml,text/yaml,text/plain" onChange={handleFileUpload} />
                </label>
                <button type="button" className="is-primary" onClick={() => applyYaml()}>
                  Render Diagram
                </button>
              </div>
            </div>

            {parseError && <pre className="yaml-editor__error">{parseError}</pre>}

            <textarea
              value={draftYaml}
              spellCheck="false"
              onChange={(event) => setDraftYaml(event.target.value)}
              aria-label="Docker Compose YAML"
            />
          </section>
        </main>
      )}

      {mode === 'inspector' && (
        <main className="workspace workspace--inspector">
          <section className="inspector-panel">
            <div>
              <h1>Parsed Topology</h1>
              <p>Normalized Compose metadata used by React Flow.</p>
            </div>
            <pre>
              {JSON.stringify(
                {
                  services: topology.parsed.services,
                  networks: topology.parsed.networks,
                  edges: topology.edges.map(({ id, source, target, label, data }) => ({
                    id,
                    source,
                    target,
                    label,
                    kind: data?.kind,
                  })),
                },
                null,
                2,
              )}
            </pre>
          </section>
        </main>
      )}
    </div>
  );
};

export default App;
