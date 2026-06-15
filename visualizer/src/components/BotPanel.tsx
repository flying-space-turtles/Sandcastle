import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  Bot,
  CheckCircle2,
  ChevronDown,
  CircleStop,
  Clock3,
  Crosshair,
  FileText,
  History,
  Plus,
  RefreshCw,
  Rocket,
  ScrollText,
  Settings2,
  ShieldCheck,
  Terminal,
  X,
  XCircle,
} from 'lucide-react';
import { botApiRequest } from '../data/operatorApi';
import type { BotActionOption, BotConfig, BotPlannerOption } from '../types';
import { DEFAULT_BOT_CONFIG } from '../types';

interface TeamStatus {
  id: number;
  running: boolean;
  pid: number | null;
  container_up: boolean;
  deployment_id?: string | null;
}

interface DeploymentEvent {
  ts: string;
  type: string;
  action_id?: string;
  target_team?: number;
  status?: string;
  code?: string;
  accepted?: boolean;
  flag_fingerprint?: string;
  message?: string;
  seconds?: number;
}

interface Deployment {
  id: string;
  team_id: number;
  bot_name: string;
  status: 'DEPLOYING' | 'RUNNING' | 'STOPPED' | 'SUPERSEDED' | 'FAILED';
  pid: number | null;
  created_at: string;
  updated_at: string;
  stopped_at: string | null;
  error: string | null;
  container_up: boolean;
  config?: Record<string, unknown>;
  summary: {
    captures: number;
    submissions: number;
    accepted: number;
    failures: number;
    last_event: DeploymentEvent | null;
    current_activity: DeploymentEvent | null;
  };
}

interface BotCatalog {
  actions: BotActionOption[];
  planners: BotPlannerOption[];
}

const FALLBACK_CATALOG: BotCatalog = {
  actions: [
    { id: 'recon.health', label: 'Health check', category: 'Recon', scope: 'target', description: 'Check target health.' },
    { id: 'exploit.path_traversal', label: 'Path traversal', category: 'Exploit', scope: 'target', description: 'Read the flag through /export.' },
    { id: 'exploit.cmdi', label: 'Command injection', category: 'Exploit', scope: 'target', description: 'Read the flag through diagnostics.' },
    { id: 'exploit.sqli', label: 'SQL injection', category: 'Exploit', scope: 'target', description: 'Bypass login and inspect notes.' },
    { id: 'probe.plant_endpoint', label: 'Plant endpoint probe', category: 'Probe', scope: 'target', description: 'Validate plant endpoint protection.' },
    { id: 'maintain.watchdog', label: 'Service watchdog', category: 'Maintenance', scope: 'self', description: 'Restart the local service when needed.' },
  ],
  planners: [
    { id: 'scripted', label: 'Scripted', description: 'Run selected actions in order.' },
    { id: 'recon_first', label: 'Recon first', description: 'Complete recon before offensive actions.' },
  ],
};

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await botApiRequest(path, options);
  const body = await response.json().catch(() => ({})) as T & { error?: string; output?: string };
  if (!response.ok) {
    const detail = body.error || body.output || `HTTP ${response.status}`;
    throw new Error(detail.length > 1200 ? `${detail.slice(0, 1200)}...` : detail);
  }
  return body;
}

const activityLabel = (event: DeploymentEvent | null) => {
  if (!event) return 'Waiting for telemetry';
  if (event.type === 'action.started') return `${event.action_id || 'Action'} on team${event.target_team}`;
  if (event.type === 'deployment.sleeping') return `Sleeping ${event.seconds || 0}s`;
  if (event.type === 'round.started') return 'Planning next round';
  return event.type.replaceAll('.', ' ');
};

const eventTitle = (event: DeploymentEvent) => {
  if (event.type === 'action.completed') return `${event.action_id} · ${event.status}`;
  if (event.type === 'submission.completed') return `Submission · ${event.code}`;
  if (event.type === 'flag.captured') return `Captured ${event.flag_fingerprint}`;
  if (event.type === 'action.started') return `Running ${event.action_id}`;
  return event.type.replaceAll('.', ' ');
};

const BotPanel = () => {
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [teams, setTeams] = useState<TeamStatus[]>([]);
  const [catalog, setCatalog] = useState<BotCatalog>(FALLBACK_CATALOG);
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedDeployment, setSelectedDeployment] = useState<Deployment | null>(null);
  const [events, setEvents] = useState<DeploymentEvent[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [config, setConfig] = useState<BotConfig>({ ...DEFAULT_BOT_CONFIG });
  const [selectedTeams, setSelectedTeams] = useState<Set<number>>(new Set());
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [filter, setFilter] = useState<'active' | 'all'>('active');

  const refresh = useCallback(async () => {
    try {
      const [deploymentBody, statusBody, catalogBody, arena] = await Promise.all([
        apiFetch<{ deployments: Deployment[] }>('/deployments'),
        apiFetch<{ teams: TeamStatus[] }>('/status'),
        apiFetch<BotCatalog>('/catalog').catch(() => FALLBACK_CATALOG),
        apiFetch<{ num_teams: number; service_port: number; ip_pattern: string }>('/arena'),
      ]);
      setDeployments(deploymentBody.deployments);
      setTeams(statusBody.teams);
      setCatalog(catalogBody);
      setConfig((current) => ({ ...current, numTeams: arena.num_teams, servicePort: arena.service_port, ipPattern: arena.ip_pattern }));
      setApiOnline(true);
    } catch (error) {
      setApiOnline(false);
      setNotice(error instanceof Error ? error.message : 'Bot controller unavailable');
    }
  }, []);

  const inspect = useCallback(async (deploymentId: string) => {
    setSelectedId(deploymentId);
    try {
      const [detail, eventBody, logBody] = await Promise.all([
        apiFetch<{ deployment: Deployment }>(`/deployments/${deploymentId}`),
        apiFetch<{ events: DeploymentEvent[] }>(`/deployments/${deploymentId}/events?limit=400`),
        apiFetch<{ lines: string[] }>(`/deployments/${deploymentId}/logs`),
      ]);
      setSelectedDeployment(detail.deployment);
      setEvents(eventBody.events);
      setLogs(logBody.lines);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Could not inspect deployment');
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) return;
    const interval = window.setInterval(() => void inspect(selectedId), 4000);
    return () => window.clearInterval(interval);
  }, [inspect, selectedId]);

  const actionGroups = useMemo(
    () => catalog.actions.reduce<Record<string, BotActionOption[]>>((groups, action) => {
      groups[action.category] = [...(groups[action.category] || []), action];
      return groups;
    }, {}),
    [catalog.actions],
  );
  const visibleDeployments = deployments.filter((deployment) =>
    filter === 'all' || ['RUNNING', 'DEPLOYING'].includes(deployment.status),
  );
  const activeCount = deployments.filter((deployment) => deployment.status === 'RUNNING').length;
  const acceptedCount = deployments.reduce((total, deployment) => total + deployment.summary.accepted, 0);
  const canDeploy = apiOnline === true && selectedTeams.size > 0 && config.actions.length > 0 && !busy;

  const toggleAction = (id: string) => setConfig((current) => ({
    ...current,
    actions: current.actions.includes(id)
      ? current.actions.filter((action) => action !== id)
      : [...current.actions, id],
  }));
  const toggleTeam = (id: number) => setSelectedTeams((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
  const toggleTarget = (id: number) => setConfig((current) => ({
    ...current,
    targetTeams: current.targetTeams.includes(id)
      ? current.targetTeams.filter((team) => team !== id)
      : [...current.targetTeams, id],
  }));

  const createDeployment = async () => {
    if (!canDeploy) return;
    setBusy(true);
    setNotice('Creating deployments…');
    try {
      const body = await apiFetch<{ deployments: Deployment[] }>('/deployments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          teams: [...selectedTeams],
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
        }),
      });
      setNotice(`${body.deployments.length} deployment${body.deployments.length === 1 ? '' : 's'} created.`);
      setCreateOpen(false);
      setSelectedTeams(new Set());
      await refresh();
      if (body.deployments[0]) await inspect(body.deployments[0].id);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Deployment failed');
    } finally {
      setBusy(false);
    }
  };

  const stopDeployment = async (deploymentId: string) => {
    setBusy(true);
    setNotice('Stopping deployment…');
    try {
      await apiFetch(`/deployments/${deploymentId}/stop`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      setNotice('Deployment stopped and telemetry archived.');
      await refresh();
      await inspect(deploymentId);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Stop failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="fleet-page">
      <section className="fleet-hero">
        <div>
          <span className="page-kicker">Automation control plane</span>
          <h1>Bot deployments</h1>
          <p>Deploy offensive workflows, follow their current work, and inspect every capture and submission.</p>
        </div>
        <div className="fleet-hero__actions">
          <span className={`connection-pill ${apiOnline ? 'is-live' : 'is-stale'}`} role="status">
            <span />{apiOnline === null ? 'Connecting' : apiOnline ? 'Controller online' : 'Controller offline'}
          </span>
          <button className="icon-button" type="button" onClick={() => void refresh()}><RefreshCw size={17} /></button>
          <button className="primary-button" type="button" disabled={apiOnline !== true} onClick={() => setCreateOpen(true)}><Plus size={17} />New deployment</button>
        </div>
      </section>

      <section className="fleet-pulse">
        <div><span className="pulse-icon is-green"><Activity size={18} /></span><p><span>Running now</span><strong>{activeCount}</strong></p></div>
        <div><span className="pulse-icon is-blue"><ShieldCheck size={18} /></span><p><span>Accepted flags</span><strong>{acceptedCount}</strong></p></div>
        <div><span className="pulse-icon is-violet"><History size={18} /></span><p><span>Deployment history</span><strong>{deployments.length}</strong></p></div>
        <div><span className="pulse-icon is-amber"><Terminal size={18} /></span><p><span>Available teams</span><strong>{teams.filter((team) => team.container_up).length}/{teams.length}</strong></p></div>
      </section>

      <div className="fleet-toolbar">
        <div className="view-switch" aria-label="Deployment filter">
          <button className={filter === 'active' ? 'is-active' : ''} onClick={() => setFilter('active')}>Active</button>
          <button className={filter === 'all' ? 'is-active' : ''} onClick={() => setFilter('all')}>History</button>
        </div>
        <span>{visibleDeployments.length} deployment{visibleDeployments.length === 1 ? '' : 's'}</span>
      </div>

      <section className="deployment-grid">
        {visibleDeployments.map((deployment) => (
          <button className={`deployment-card status-${deployment.status.toLowerCase()}`} key={deployment.id} onClick={() => void inspect(deployment.id)}>
            <div className="deployment-card__top">
              <span className="team-orb">T{deployment.team_id}</span>
              <div><strong>{deployment.bot_name}</strong><span>team{deployment.team_id} · {deployment.id}</span></div>
              <span className="deployment-status">{deployment.status}</span>
            </div>
            <div className="deployment-activity">
              <Activity size={15} />
              <span>{activityLabel(deployment.summary.current_activity)}</span>
            </div>
            <div className="deployment-stats">
              <span><b>{deployment.summary.captures}</b>captures</span>
              <span><b>{deployment.summary.accepted}</b>accepted</span>
              <span><b>{deployment.summary.failures}</b>errors</span>
            </div>
            <div className="deployment-card__footer">
              <span><Clock3 size={12} />{new Date(deployment.created_at).toLocaleString()}</span>
              <span>{deployment.container_up ? 'Container online' : 'Container offline'}</span>
            </div>
          </button>
        ))}
        {visibleDeployments.length === 0 && (
          <div className="fleet-empty">
            <Bot size={28} />
            <h2>{filter === 'active' ? 'No active deployments' : 'No deployment history'}</h2>
            <p>Create a bot deployment to begin automated arena operations.</p>
            <button className="primary-button" disabled={apiOnline !== true} onClick={() => setCreateOpen(true)}><Plus size={16} />New deployment</button>
          </div>
        )}
      </section>

      <div className="sr-status" role="status" aria-live="polite">{notice}</div>

      {createOpen && (
        <div className="drawer-backdrop" onMouseDown={() => setCreateOpen(false)}>
          <aside className="ops-drawer ops-drawer--wide" onMouseDown={(event) => event.stopPropagation()} aria-label="New bot deployment">
            <div className="drawer-heading">
              <div><span className="section-icon"><Rocket size={18} /></span><div><h2>New deployment</h2><p>Configure once and deploy independently to each team.</p></div></div>
              <button className="icon-button" onClick={() => setCreateOpen(false)}><X size={18} /></button>
            </div>
            <div className="deployment-form">
              <div className="form-row">
                <label className="drawer-field"><span>Bot name</span><input value={config.botName} onChange={(event) => setConfig((current) => ({ ...current, botName: event.target.value }))} /></label>
                <label className="drawer-field"><span>Planner</span><select value={config.planner} onChange={(event) => setConfig((current) => ({ ...current, planner: event.target.value }))}>{catalog.planners.map((planner) => <option key={planner.id} value={planner.id}>{planner.label}</option>)}</select></label>
              </div>
              <fieldset className="choice-fieldset">
                <legend><Bot size={15} />Deploy as teams</legend>
                <div className="choice-grid">
                  {teams.map((team) => (
                    <label className={`${selectedTeams.has(team.id) ? 'is-selected' : ''} ${!team.container_up ? 'is-disabled' : ''}`} key={team.id}>
                      <input type="checkbox" checked={selectedTeams.has(team.id)} disabled={!team.container_up} onChange={() => toggleTeam(team.id)} />
                      <span className="team-orb">T{team.id}</span><b>team{team.id}</b><small>{team.container_up ? team.running ? 'Bot running' : 'Ready' : 'Offline'}</small>
                    </label>
                  ))}
                </div>
              </fieldset>
              <fieldset className="choice-fieldset">
                <legend><ShieldCheck size={15} />Actions</legend>
                {Object.entries(actionGroups).map(([group, actions]) => (
                  <div className="action-choice-group" key={group}>
                    <span>{group}</span>
                    <div>{actions.map((action) => <label className={config.actions.includes(action.id) ? 'is-selected' : ''} key={action.id} title={action.description}><input type="checkbox" checked={config.actions.includes(action.id)} onChange={() => toggleAction(action.id)} />{action.label}</label>)}</div>
                  </div>
                ))}
              </fieldset>
              <fieldset className="choice-fieldset">
                <legend><Crosshair size={15} />Targets</legend>
                <div className="view-switch">
                  <button className={config.targetPolicy === 'all_opponents' ? 'is-active' : ''} onClick={() => setConfig((current) => ({ ...current, targetPolicy: 'all_opponents' }))}>All opponents</button>
                  <button className={config.targetPolicy === 'selected' ? 'is-active' : ''} onClick={() => setConfig((current) => ({ ...current, targetPolicy: 'selected' }))}>Selected</button>
                </div>
                {config.targetPolicy === 'selected' && <div className="target-chips">{teams.map((team) => <label className={config.targetTeams.includes(team.id) ? 'is-selected' : ''} key={team.id}><input type="checkbox" checked={config.targetTeams.includes(team.id)} onChange={() => toggleTarget(team.id)} />team{team.id}</label>)}</div>}
              </fieldset>
              <button className="advanced-toggle" onClick={() => setAdvancedOpen((open) => !open)}><Settings2 size={15} />Advanced runtime settings<ChevronDown className={advancedOpen ? 'is-open' : ''} size={15} /></button>
              {advancedOpen && (
                <div className="advanced-grid">
                  <label className="drawer-field"><span>Loop interval</span><input type="number" min={0} value={config.loopInterval} onChange={(event) => setConfig((current) => ({ ...current, loopInterval: Number(event.target.value) }))} /></label>
                  <label className="drawer-field"><span>Request timeout</span><input type="number" min={1} value={config.timeout} onChange={(event) => setConfig((current) => ({ ...current, timeout: Number(event.target.value) }))} /></label>
                  <label className="drawer-field is-wide"><span>Flag pattern</span><input value={config.flagRe} onChange={(event) => setConfig((current) => ({ ...current, flagRe: event.target.value }))} /></label>
                  <label className="check-field"><input type="checkbox" checked={config.stopOnSuccess} onChange={(event) => setConfig((current) => ({ ...current, stopOnSuccess: event.target.checked }))} />Stop exploit chain after successful capture</label>
                </div>
              )}
            </div>
            <div className="drawer-footer">
              <span>{selectedTeams.size} team{selectedTeams.size === 1 ? '' : 's'} selected</span>
              <button className="primary-button" disabled={!canDeploy} onClick={() => void createDeployment()}><Rocket size={16} />{busy ? 'Deploying…' : 'Deploy bot'}</button>
            </div>
          </aside>
        </div>
      )}

      {selectedId && (
        <div className="drawer-backdrop" onMouseDown={() => setSelectedId(null)}>
          <aside className="ops-drawer ops-drawer--inspect" onMouseDown={(event) => event.stopPropagation()} aria-label="Deployment details">
            <div className="drawer-heading">
              <div><span className="team-orb">T{selectedDeployment?.team_id || '?'}</span><div><h2>{selectedDeployment?.bot_name || 'Deployment'}</h2><p>{selectedId}</p></div></div>
              <button className="icon-button" onClick={() => setSelectedId(null)}><X size={18} /></button>
            </div>
            {selectedDeployment && (
              <>
                <div className="inspect-status">
                  <div><span className={`large-status status-${selectedDeployment.status.toLowerCase()}`}>{selectedDeployment.status}</span><small>{selectedDeployment.pid ? `PID ${selectedDeployment.pid}` : 'No active process'}</small></div>
                  {selectedDeployment.status === 'RUNNING' && <button className="danger-button" disabled={busy} onClick={() => void stopDeployment(selectedDeployment.id)}><CircleStop size={15} />Stop</button>}
                </div>
                <div className="inspect-metrics">
                  <span><b>{selectedDeployment.summary.captures}</b>Captured</span>
                  <span><b>{selectedDeployment.summary.submissions}</b>Submitted</span>
                  <span><b>{selectedDeployment.summary.accepted}</b>Accepted</span>
                  <span><b>{selectedDeployment.summary.failures}</b>Errors</span>
                </div>
                <div className="inspect-section">
                  <h3><Activity size={15} />Timeline</h3>
                  <div className="event-timeline">
                    {[...events].reverse().slice(0, 100).map((event, index) => (
                      <div className={`event-row ${event.accepted ? 'is-success' : ''}`} key={`${event.ts}-${index}`}>
                        <span>{event.accepted ? <CheckCircle2 size={14} /> : event.type.includes('failed') ? <XCircle size={14} /> : <Activity size={14} />}</span>
                        <div><strong>{eventTitle(event)}</strong><small>{event.message || (event.target_team ? `team${event.target_team}` : '')}</small></div>
                        <time>{new Date(event.ts).toLocaleTimeString()}</time>
                      </div>
                    ))}
                    {events.length === 0 && <div className="empty-state">No structured events yet.</div>}
                  </div>
                </div>
                <details className="inspect-section inspect-details">
                  <summary><FileText size={15} />Configuration<ChevronDown size={14} /></summary>
                  <pre>{JSON.stringify(selectedDeployment.config, null, 2)}</pre>
                </details>
                <details className="inspect-section inspect-details" open>
                  <summary><ScrollText size={15} />Raw logs<ChevronDown size={14} /></summary>
                  <pre className="deployment-log">{logs.join('\n') || 'No log output yet.'}</pre>
                </details>
              </>
            )}
          </aside>
        </div>
      )}
    </main>
  );
};

export default BotPanel;
