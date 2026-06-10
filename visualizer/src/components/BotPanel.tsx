import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Bot,
  CircleStop,
  Crosshair,
  RefreshCw,
  Rocket,
  ScrollText,
  ShieldCheck,
  SlidersHorizontal,
  Terminal,
} from 'lucide-react';
import { botApiUrl } from '../data/arenaConfig';
import type { BotActionOption, BotConfig, BotPlannerOption } from '../types';
import { DEFAULT_BOT_CONFIG } from '../types';

interface TeamStatus {
  id: number;
  running: boolean;
  pid: number | null;
  container_up: boolean;
}

interface BotCatalog {
  actions: BotActionOption[];
  planners: BotPlannerOption[];
}

interface ArenaBotConfig {
  num_teams: number;
  service_port: number;
  ip_pattern: string;
  ssh_base_port: number;
}

const FALLBACK_CATALOG: BotCatalog = {
  actions: [
    {
      id: 'recon.health',
      label: 'Health check',
      category: 'Recon',
      scope: 'target',
      description: 'GET /health before heavier actions.',
    },
    {
      id: 'exploit.path_traversal',
      label: 'Path traversal',
      category: 'Exploit',
      scope: 'target',
      description: 'Read ../flag.txt through /export.',
    },
    {
      id: 'exploit.cmdi',
      label: 'Command injection',
      category: 'Exploit',
      scope: 'target',
      description: 'Inject a flag read through /admin/diagnostics.',
    },
    {
      id: 'exploit.sqli',
      label: 'SQL injection',
      category: 'Exploit',
      scope: 'target',
      description: 'Bypass login as admin and read /notes.',
    },
    {
      id: 'probe.plant_endpoint',
      label: 'Plant endpoint probe',
      category: 'Probe',
      scope: 'target',
      description: 'Probe /internal/plant with an invalid token.',
    },
    {
      id: 'maintain.watchdog',
      label: 'Service watchdog',
      category: 'Maintenance',
      scope: 'self',
      description: 'Restart this team service if Docker access is available.',
    },
  ],
  planners: [
    {
      id: 'scripted',
      label: 'Scripted',
      description: 'Run selected actions in order against each eligible opponent.',
    },
    {
      id: 'recon_first',
      label: 'Recon, then exploits',
      description: 'Run recon across all targets, then run offensive actions.',
    },
  ],
};

async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${botApiUrl}${path}`, opts);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

const StatusDot = ({ up, title }: { up: boolean; title: string }) => (
  <span className={`bot-status-dot ${up ? 'is-up' : 'is-down'}`} title={title} />
);

const TeamCard = ({
  team,
  selected,
  onToggle,
}: {
  team: TeamStatus;
  selected: boolean;
  onToggle: (id: number) => void;
}) => (
  <label className={`bot-team-card ${selected ? 'is-selected' : ''} ${!team.container_up ? 'is-offline' : ''}`}>
    <input type="checkbox" checked={selected} onChange={() => onToggle(team.id)} disabled={!team.container_up} />
    <div className="bot-team-card__body">
      <div className="bot-team-card__name">
        <StatusDot up={team.container_up} title={team.container_up ? 'Container up' : 'Container offline'} />
        team{team.id}
      </div>
      <div className="bot-team-card__state">
        {!team.container_up ? (
          <span className="bot-badge is-offline">offline</span>
        ) : team.running ? (
          <span className="bot-badge is-running">running{team.pid ? ` pid ${team.pid}` : ''}</span>
        ) : (
          <span className="bot-badge is-idle">idle</span>
        )}
      </div>
    </div>
  </label>
);

const groupActions = (actions: BotActionOption[]) =>
  actions.reduce<Record<string, BotActionOption[]>>((acc, action) => {
    acc[action.category] = [...(acc[action.category] || []), action];
    return acc;
  }, {});

const BotPanel = () => {
  const [config, setConfig] = useState<BotConfig>({ ...DEFAULT_BOT_CONFIG });
  const [catalog, setCatalog] = useState<BotCatalog>(FALLBACK_CATALOG);
  const [teams, setTeams] = useState<TeamStatus[]>([]);
  const [selectedDeployTeams, setSelectedDeployTeams] = useState<Set<number>>(new Set());
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<string[]>([]);
  const [logTeam, setLogTeam] = useState<number | null>(null);
  const logRef = useRef<HTMLPreElement>(null);

  const onlineTeams = useMemo(() => teams.filter((team) => team.container_up), [teams]);
  const actionGroups = useMemo(() => groupActions(catalog.actions), [catalog.actions]);
  const actionOrder = useMemo(() => catalog.actions.map((action) => action.id), [catalog.actions]);
  const builtinPlanners = useMemo(
    () => catalog.planners.filter((planner) => !planner.id.startsWith('module:')),
    [catalog.planners],
  );
  const deployTeamCount = selectedDeployTeams.size;
  const selectedActions = config.actions
    .map((id) => catalog.actions.find((action) => action.id === id))
    .filter(Boolean) as BotActionOption[];
  const targetActionCount = selectedActions.filter((action) => action.scope === 'target').length;
  const canDeploy =
    apiOnline === true &&
    deployTeamCount > 0 &&
    targetActionCount > 0 &&
    (config.targetPolicy !== 'selected' || config.targetTeams.length > 0) &&
    !busy;

  const refreshStatus = useCallback(async () => {
    try {
      const [status, nextCatalog, arena] = await Promise.all([
        apiFetch<{ teams: TeamStatus[] }>('/status'),
        apiFetch<BotCatalog>('/catalog').catch(() => FALLBACK_CATALOG),
        apiFetch<ArenaBotConfig>('/arena'),
      ]);
      setTeams(status.teams);
      setCatalog(nextCatalog);
      setApiOnline(true);
      setConfig((current) => ({
        ...current,
        numTeams: arena.num_teams,
        servicePort: arena.service_port,
        ipPattern: arena.ip_pattern,
      }));
    } catch {
      setApiOnline(false);
    }
  }, []);

  useEffect(() => {
    refreshStatus();
    const id = setInterval(refreshStatus, 4000);
    return () => clearInterval(id);
  }, [refreshStatus]);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [log]);

  const appendLog = (lines: string) => {
    setLog((prev) => [...prev.slice(-500), ...lines.split('\n').filter(Boolean)]);
  };

  const toggleDeployTeam = (id: number) =>
    setSelectedDeployTeams((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const toggleTargetTeam = (id: number) =>
    setConfig((prev) => ({
      ...prev,
      targetTeams: prev.targetTeams.includes(id)
        ? prev.targetTeams.filter((teamId) => teamId !== id)
        : [...prev.targetTeams, id].sort((a, b) => a - b),
    }));

  const toggleAction = (id: string) =>
    setConfig((prev) => {
      const nextActions = prev.actions.includes(id)
        ? prev.actions.filter((actionId) => actionId !== id)
        : [...prev.actions, id];

      return {
        ...prev,
        actions: nextActions.sort((a, b) => actionOrder.indexOf(a) - actionOrder.indexOf(b)),
      };
    });

  const selectAllDeployTeams = () => setSelectedDeployTeams(new Set(onlineTeams.map((team) => team.id)));
  const selectNoDeployTeams = () => setSelectedDeployTeams(new Set());

  const handleDeploy = async () => {
    if (!canDeploy) {
      return;
    }
    setBusy(true);
    setLog([]);
    try {
      const payload = {
        teams: [...selectedDeployTeams],
        bot_name: config.botName,
        planner: config.planner,
        target_policy: config.targetPolicy,
        target_teams: config.targetTeams,
        actions: config.actions,
        loop_interval: config.loopInterval,
        watchdog: config.actions.includes('maintain.watchdog'),
        flag_re: config.flagRe,
        stop_on_success: config.stopOnSuccess,
        timeout: config.timeout,
      };
      const res = await apiFetch<{ ok: boolean; output: string }>('/deploy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      appendLog(res.output);
      appendLog(res.ok ? 'Deploy succeeded' : 'Deploy reported errors');
      await refreshStatus();
    } catch (error) {
      appendLog(`Error: ${error}`);
    } finally {
      setBusy(false);
    }
  };

  const handleStop = async () => {
    if (deployTeamCount === 0) {
      return;
    }
    setBusy(true);
    try {
      const res = await apiFetch<{ ok: boolean; output: string }>('/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ teams: [...selectedDeployTeams] }),
      });
      appendLog(res.output);
      await refreshStatus();
    } catch (error) {
      appendLog(`Error: ${error}`);
    } finally {
      setBusy(false);
    }
  };

  const handleFetchLogs = async (teamId: number) => {
    setLogTeam(teamId);
    try {
      const res = await apiFetch<{ lines: string[] }>(`/logs/${teamId}`);
      setLog(res.lines.length > 0 ? res.lines : ['(no log output yet)']);
    } catch (error) {
      setLog([`Error: ${error}`]);
    }
  };

  return (
    <main className="bot-panel">
      <div className="bot-panel__header">
        <div className="bot-panel__heading">
          <Bot size={22} />
          <div>
            <h1>Bot Workshop</h1>
            <p>Configure a bot, then deploy it as one or more teams.</p>
          </div>
        </div>
        <div className="bot-api-status">
          <span className={`bot-status-dot ${apiOnline === true ? 'is-up' : 'is-down'}`} />
          {apiOnline === null ? 'Connecting' : apiOnline ? 'API online' : 'API offline'}
        </div>
      </div>

      <div className="bot-panel__body">
        <div className="bot-panel__left">
          <section className="bot-section">
            <div className="bot-section__titlebar">
              <h2 className="bot-section__title">
                <Bot size={15} />
                Bot
              </h2>
            </div>

            <label className="bot-field bot-field--full">
              <span>Name</span>
              <input
                type="text"
                value={config.botName}
                onChange={(event) => setConfig((current) => ({ ...current, botName: event.target.value }))}
              />
            </label>

            <label className="bot-field bot-field--full">
              <span>Planner</span>
              <select
                value={config.planner}
                onChange={(event) => setConfig((current) => ({ ...current, planner: event.target.value }))}
              >
                {builtinPlanners.map((planner) => (
                  <option key={planner.id} value={planner.id}>
                    {planner.label}
                  </option>
                ))}
              </select>
            </label>
          </section>

          <section className="bot-section">
            <h2 className="bot-section__title">
              <SlidersHorizontal size={15} />
              Runtime
            </h2>

            <div className="bot-field-grid">
              <label className="bot-field">
                <span>Loop seconds</span>
                <input
                  type="number"
                  min={0}
                  value={config.loopInterval}
                  onChange={(event) =>
                    setConfig((current) => ({ ...current, loopInterval: Number(event.target.value) }))
                  }
                />
              </label>
              <label className="bot-field">
                <span>Total teams</span>
                <input
                  type="number"
                  min={1}
                  value={config.numTeams}
                  readOnly
                  title="Configured in config/arena.env"
                />
              </label>
              <label className="bot-field">
                <span>Service port</span>
                <input
                  type="number"
                  min={1}
                  max={65535}
                  value={config.servicePort}
                  readOnly
                  title="Configured in config/arena.env"
                />
              </label>
              <label className="bot-field">
                <span>Timeout</span>
                <input
                  type="number"
                  min={1}
                  value={config.timeout}
                  onChange={(event) => setConfig((current) => ({ ...current, timeout: Number(event.target.value) }))}
                />
              </label>
              <label className="bot-field bot-field--full">
                <span>IP pattern</span>
                <input
                  type="text"
                  value={config.ipPattern}
                  readOnly
                  title="Derived from config/arena.env"
                />
              </label>
              <label className="bot-field bot-field--full">
                <span>Flag regex</span>
                <input
                  type="text"
                  value={config.flagRe}
                  onChange={(event) => setConfig((current) => ({ ...current, flagRe: event.target.value }))}
                />
              </label>
            </div>

            <label className="bot-toggle">
              <input
                type="checkbox"
                checked={config.stopOnSuccess}
                onChange={(event) => setConfig((current) => ({ ...current, stopOnSuccess: event.target.checked }))}
              />
              Stop exploit chain after a flag
            </label>
          </section>

          <section className="bot-section">
            <h2 className="bot-section__title">
              <Activity size={15} />
              Actions
            </h2>

            {Object.entries(actionGroups).map(([category, actions]) => (
              <div key={category} className="bot-action-group">
                <div className="bot-action-group__title">{category}</div>
                <div className="bot-action-list">
                  {actions.map((action) => {
                    const checked = config.actions.includes(action.id);
                    return (
                      <label
                        key={action.id}
                        className={checked ? 'is-selected' : ''}
                        title={action.description}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleAction(action.id)}
                        />
                        <ShieldCheck size={14} />
                        <span>{action.label}</span>
                      </label>
                    );
                  })}
                </div>
              </div>
            ))}
          </section>

          <section className="bot-section">
            <h2 className="bot-section__title">
              <Crosshair size={15} />
              Opponents
            </h2>

            <div className="bot-segmented" aria-label="Target policy">
              <button
                type="button"
                className={config.targetPolicy === 'all_opponents' ? 'is-active' : ''}
                onClick={() => setConfig((current) => ({ ...current, targetPolicy: 'all_opponents' }))}
              >
                All opponents
              </button>
              <button
                type="button"
                className={config.targetPolicy === 'selected' ? 'is-active' : ''}
                onClick={() => setConfig((current) => ({ ...current, targetPolicy: 'selected' }))}
              >
                Selected
              </button>
            </div>

            {config.targetPolicy === 'selected' && (
              <div className="bot-target-grid">
                {teams.map((team) => (
                  <label key={team.id} className="bot-target-chip">
                    <input
                      type="checkbox"
                      checked={config.targetTeams.includes(team.id)}
                      onChange={() => toggleTargetTeam(team.id)}
                    />
                    team{team.id}
                  </label>
                ))}
              </div>
            )}
          </section>

          <section className="bot-section">
            <div className="bot-section__titlebar">
              <h2 className="bot-section__title">
                <Rocket size={15} />
                Deploy As Team
              </h2>
              <div className="bot-section__actions">
                <button type="button" onClick={selectAllDeployTeams}>
                  All
                </button>
                <button type="button" onClick={selectNoDeployTeams}>
                  None
                </button>
              </div>
            </div>

            {teams.length === 0 ? (
              <p className="bot-empty">
                {apiOnline === false ? 'Start bot/bot_api.py to deploy bots.' : 'No running teams detected.'}
              </p>
            ) : (
              <div className="bot-team-grid">
                {teams.map((team) => (
                  <TeamCard
                    key={team.id}
                    team={team}
                    selected={selectedDeployTeams.has(team.id)}
                    onToggle={toggleDeployTeam}
                  />
                ))}
              </div>
            )}

            <div className="bot-deploy-bar">
              <button type="button" className="bot-btn is-primary" disabled={!canDeploy} onClick={handleDeploy}>
                <Rocket size={15} />
                {busy ? 'Working' : `Deploy ${deployTeamCount}`}
              </button>
              <button
                type="button"
                className="bot-btn is-danger"
                disabled={deployTeamCount === 0 || busy || apiOnline !== true}
                onClick={handleStop}
              >
                <CircleStop size={15} />
                Stop
              </button>
              <button type="button" className="bot-btn" disabled={apiOnline !== true} onClick={refreshStatus}>
                <RefreshCw size={15} />
                Refresh
              </button>
            </div>
          </section>
        </div>

        <div className="bot-panel__right">
          <section className="bot-section bot-section--plan">
            <h2 className="bot-section__title">
              <Terminal size={15} />
              Deployment Plan
            </h2>
            <div className="bot-plan">
              <div>
                <span>Bot</span>
                <strong>{config.botName || 'Unnamed bot'}</strong>
              </div>
              <div>
                <span>Deploy</span>
                <strong>{deployTeamCount > 0 ? [...selectedDeployTeams].map((id) => `team${id}`).join(', ') : 'None'}</strong>
              </div>
              <div>
                <span>Targets</span>
                <strong>
                  {config.targetPolicy === 'all_opponents'
                    ? 'All opponents'
                    : config.targetTeams.map((id) => `team${id}`).join(', ') || 'None'}
                </strong>
              </div>
              <div>
                <span>Actions</span>
                <strong>{selectedActions.map((action) => action.label).join(' -> ') || 'None'}</strong>
              </div>
            </div>
          </section>

          <section className="bot-section bot-section--logs">
            <div className="bot-section__titlebar">
              <h2 className="bot-section__title">
                <ScrollText size={15} />
                Logs{logTeam !== null ? ` team${logTeam}` : ''}
              </h2>
              <div className="bot-section__actions">
                {onlineTeams.map((team) => (
                  <button
                    key={team.id}
                    type="button"
                    className={logTeam === team.id ? 'is-active' : ''}
                    onClick={() => handleFetchLogs(team.id)}
                  >
                    team{team.id}
                  </button>
                ))}
                {logTeam !== null && (
                  <button type="button" title="Refresh log" onClick={() => handleFetchLogs(logTeam)}>
                    <RefreshCw size={12} />
                  </button>
                )}
                <button type="button" onClick={() => setLog([])}>
                  Clear
                </button>
              </div>
            </div>

            {log.length === 0 ? (
              <p className="bot-empty">Select a team log or deploy a bot.</p>
            ) : (
              <pre className="bot-log" ref={logRef}>
                {log.join('\n')}
              </pre>
            )}
          </section>
        </div>
      </div>
    </main>
  );
};

export default BotPanel;
