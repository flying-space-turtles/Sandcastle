import { useCallback, useMemo, useState } from 'react';
import defaultComposeYaml from '../../docker-compose.yml?raw';
import exampleComposeYaml from '../../services/example-vuln/docker-compose.yml?raw';
import sshDockerfile from '../../teams/team1/ssh/Dockerfile?raw';
import vulnDockerfile from '../../services/example-vuln/Dockerfile?raw';
import DetailsPanel from './components/DetailsPanel.jsx';
import DockerCanvas from './components/DockerCanvas.jsx';
import TopologyNav from './components/TopologyNav.jsx';
import { parseDockerCompose } from './data/dockerComposeParser.js';
import { buildDockerFlow } from './graph/dockerGraph.js';

const dockerfileSources = {
  'ssh/Dockerfile': sshDockerfile,
  './ssh/Dockerfile': sshDockerfile,
  'teams/team1/ssh/Dockerfile': sshDockerfile,
  './teams/team1/ssh/Dockerfile': sshDockerfile,
  'teams/team2/ssh/Dockerfile': sshDockerfile,
  './teams/team2/ssh/Dockerfile': sshDockerfile,
  'teams/team3/ssh/Dockerfile': sshDockerfile,
  './teams/team3/ssh/Dockerfile': sshDockerfile,
  'teams/team4/ssh/Dockerfile': sshDockerfile,
  './teams/team4/ssh/Dockerfile': sshDockerfile,
  'teams/team5/ssh/Dockerfile': sshDockerfile,
  './teams/team5/ssh/Dockerfile': sshDockerfile,
  'teams/team6/ssh/Dockerfile': sshDockerfile,
  './teams/team6/ssh/Dockerfile': sshDockerfile,
  'services/example-vuln/Dockerfile': vulnDockerfile,
  './services/example-vuln/Dockerfile': vulnDockerfile,
  'teams/team1/service/Dockerfile': vulnDockerfile,
  './teams/team1/service/Dockerfile': vulnDockerfile,
  'teams/team2/service/Dockerfile': vulnDockerfile,
  './teams/team2/service/Dockerfile': vulnDockerfile,
  'teams/team3/service/Dockerfile': vulnDockerfile,
  './teams/team3/service/Dockerfile': vulnDockerfile,
  'teams/team4/service/Dockerfile': vulnDockerfile,
  './teams/team4/service/Dockerfile': vulnDockerfile,
  'teams/team5/service/Dockerfile': vulnDockerfile,
  './teams/team5/service/Dockerfile': vulnDockerfile,
  'teams/team6/service/Dockerfile': vulnDockerfile,
  './teams/team6/service/Dockerfile': vulnDockerfile,
  Dockerfile: vulnDockerfile,
  './Dockerfile': vulnDockerfile,
};

const buildTopology = (yamlSource) => {
  const parsed = parseDockerCompose(yamlSource, dockerfileSources);
  const flow = buildDockerFlow(parsed);

  return {
    parsed,
    ...flow,
  };
};

const App = () => {
  const [mode, setMode] = useState('editor');
  const [draftYaml, setDraftYaml] = useState(defaultComposeYaml);
  const [topology, setTopology] = useState(() => buildTopology(defaultComposeYaml));
  const [parseError, setParseError] = useState(null);
  const [selectedNode, setSelectedNode] = useState(null);

  const summary = useMemo(
    () => ({
      serviceCount: topology.parsed.services.length,
      networkCount: topology.parsed.networks.length || 1,
      edgeCount: topology.edges.length,
    }),
    [topology],
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
            <DockerCanvas topology={topology} onSelectNode={handleSelectNode} />
          </section>
          <DetailsPanel node={selectedNode} />
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
