import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import defaultComposeYaml from '../../docker-compose.yml?raw';
import exampleComposeYaml from '../../services/example-vuln/docker-compose.yml?raw';
import sshDockerfile from '../../docker/ssh/Dockerfile?raw';
import vulnDockerfile from '../../services/example-vuln/Dockerfile?raw';
import DockerCanvas from './components/DockerCanvas.jsx';
import RightPanel from './components/RightPanel.jsx';
import TopologyNav from './components/TopologyNav.jsx';
import { parseDockerCompose } from './data/dockerComposeParser.js';
import { buildDockerFlow } from './graph/dockerGraph.js';

const dockerfileSources = {
  'ssh/Dockerfile': sshDockerfile,
  './ssh/Dockerfile': sshDockerfile,
  'docker/ssh/Dockerfile': sshDockerfile,
  './docker/ssh/Dockerfile': sshDockerfile,
  'services/example-vuln/Dockerfile': vulnDockerfile,
  './services/example-vuln/Dockerfile': vulnDockerfile,
  'teams/generated/team1/service/Dockerfile': vulnDockerfile,
  './teams/generated/team1/service/Dockerfile': vulnDockerfile,
  'teams/generated/team2/service/Dockerfile': vulnDockerfile,
  './teams/generated/team2/service/Dockerfile': vulnDockerfile,
  'teams/generated/team3/service/Dockerfile': vulnDockerfile,
  './teams/generated/team3/service/Dockerfile': vulnDockerfile,
  'teams/generated/team4/service/Dockerfile': vulnDockerfile,
  './teams/generated/team4/service/Dockerfile': vulnDockerfile,
  'teams/generated/team5/service/Dockerfile': vulnDockerfile,
  './teams/generated/team5/service/Dockerfile': vulnDockerfile,
  'teams/generated/team6/service/Dockerfile': vulnDockerfile,
  './teams/generated/team6/service/Dockerfile': vulnDockerfile,
  Dockerfile: vulnDockerfile,
  './Dockerfile': vulnDockerfile,
};

const buildTopology = (yamlSource) => {
  const parsed = parseDockerCompose(yamlSource, dockerfileSources);
  const flow = buildDockerFlow(parsed);
  return { parsed, ...flow };
};

/** Stamp isBot=true on SSH-role nodes whose teamId is in botTeams. */
const applyBotFlags = (nodes, botTeams) => {
  if (!botTeams.length) return nodes;
  const botSet = new Set(botTeams.map(String));
  return nodes.map((n) => {
    const isBot = botSet.has(String(n.data?.teamId)) && n.data?.relationRole === 'ssh';
    if (isBot === Boolean(n.data?.isBot)) return n;
    return { ...n, data: { ...n.data, isBot } };
  });
};

const POLL_INTERVAL = 3000;
const FLASH_TTL = 4000; // ms — how long an attack arrow stays visible

const FLASH_COLOR = {
  flag:      '#34d399',
  probe:     '#60a5fa',
  fail:      '#f87171',
  ping_up:   '#4ade80',
  ping_down: '#f87171',
};

const App = () => {
  const [mode, setMode] = useState('editor');
  const [draftYaml, setDraftYaml] = useState(defaultComposeYaml);
  const [topology, setTopology] = useState(() => buildTopology(defaultComposeYaml));
  const [parseError, setParseError] = useState(null);
  const [selectedNode, setSelectedNode] = useState(null);

  // Bot state from event server
  const [botTeams, setBotTeams] = useState([]);
  const [events, setEvents] = useState([]);
  const [serverConnected, setServerConnected] = useState(false);
  const pollRef = useRef(null);

  useEffect(() => {
    const poll = async () => {
      try {
        const res = await fetch('/api/state');
        if (!res.ok) throw new Error('non-200');
        const data = await res.json();
        setBotTeams(data.botTeams ?? []);
        setEvents(data.events ?? []);
        setServerConnected(true);
      } catch {
        setServerConnected(false);
      }
    };
    poll();
    pollRef.current = setInterval(poll, POLL_INTERVAL);
    return () => clearInterval(pollRef.current);
  }, []);

  // Flash edges — temporary animated arrows on the canvas for attack/ping events
  const [flashEdges, setFlashEdges] = useState([]);
  const seenEventKeys = useRef(new Set());

  useEffect(() => {
    const now = Date.now();
    const newFlash = [];
    for (const ev of events) {
      const key = `${ev.ts}-${ev.attacker}-${ev.type}-${ev.victim ?? ''}`;
      if (seenEventKeys.current.has(key)) continue;
      seenEventKeys.current.add(key);
      if (!ev.victim) continue;
      if (!FLASH_COLOR[ev.type]) continue;
      const color = FLASH_COLOR[ev.type];
      newFlash.push({
        id: `flash-${key}`,
        source: `${ev.attacker}-ssh`,
        target: `${ev.victim}-vuln`,
        animated: true,
        markerEnd: { type: 'arrowclosed', color, width: 16, height: 16 },
        style: { stroke: color, strokeDasharray: '6 3', strokeWidth: 2.6, strokeOpacity: 0.9 },
        data: { kind: 'flash', eventType: ev.type },
        expiresAt: now + FLASH_TTL,
      });
    }
    if (newFlash.length) {
      setFlashEdges((prev) => [...prev, ...newFlash]);
    }
  }, [events]);

  // Clean up expired flash edges every second
  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now();
      setFlashEdges((prev) => {
        const next = prev.filter((e) => e.expiresAt > now);
        return next.length === prev.length ? prev : next;
      });
    }, 1000);
    return () => clearInterval(id);
  }, []);

  // Enrich topology nodes with isBot flag whenever botTeams changes
  const enrichedTopology = useMemo(
    () => ({ ...topology, nodes: applyBotFlags(topology.nodes, botTeams) }),
    [topology, botTeams],
  );

  const summary = useMemo(
    () => ({
      serviceCount: enrichedTopology.parsed.services.length,
      networkCount: enrichedTopology.parsed.networks.length || 1,
      edgeCount: enrichedTopology.edges.length,
    }),
    [enrichedTopology],
  );

  const applyYaml = useCallback(
    (yamlSource = draftYaml) => {
      try {
        const nextTopology = buildTopology(yamlSource);
        setTopology(nextTopology);
        setDraftYaml(yamlSource);
        setParseError(null);
        setSelectedNode(null);
        setMode('editor');
      } catch (error) {
        setParseError(error.message);
        setMode('yaml');
      }
    },
    [draftYaml],
  );

  const handlePreset = (yamlSource) => {
    setDraftYaml(yamlSource);
    applyYaml(yamlSource);
  };

  const handleFileUpload = async (event) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }

    const text = await file.text();
    setDraftYaml(text);
    applyYaml(text);
    event.target.value = '';
  };

  const handleSelectNode = useCallback((node) => {
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
      />

      {mode === 'editor' && (
        <main className="workspace workspace--canvas">
          <section className="canvas-shell">
            <DockerCanvas topology={enrichedTopology} onSelectNode={handleSelectNode} flashEdges={flashEdges} />
          </section>
          <RightPanel node={selectedNode} events={events} connected={serverConnected} />
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
                  services: enrichedTopology.parsed.services,
                  networks: enrichedTopology.parsed.networks,
                  edges: enrichedTopology.edges.map(({ id, source, target, label, data }) => ({
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
