import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity, Brain, CheckCircle2, FileCode2, FlaskConical,
  ListTree, Plus, RefreshCw, Rocket, ScrollText, ShieldCheck, Swords, Target, Trash2, X,
} from 'lucide-react';
import { botApiRequest } from '../data/operatorApi';

// ── Types ────────────────────────────────────────────────────────────────────

interface Provider { id: string; label: string; available: boolean; models?: string[]; }

const DEFAULT_PROVIDERS: Provider[] = [
  { id: 'fake', label: 'Fake (offline)', available: true, models: ['fake-v1'] },
  { id: 'openai', label: 'OpenAI', available: false, models: ['gpt-5.4-mini', 'gpt-4o-mini'] },
  { id: 'gemini', label: 'Google Gemini', available: false, models: ['gemini-2.5-flash-lite', 'gemini-2.5-flash', 'gemini-1.5-pro'] },
];

interface VulnOption { id: string; label: string; icon: string; description: string; }
interface DiffOption { id: string; label: string; }
interface ChallengeOptions {
  vulnerabilities: VulnOption[];
  difficulties: DiffOption[];
  decoy_range: { min: number; max: number };
}

interface ChallengeRun {
  id: string;
  challenge_id: string | null;
  status: 'running' | 'published' | 'failed' | 'cancelled';
  vulnerability: string;
  difficulty: string;
  seed: number;
  decoy_endpoints: number;
  provider: string;
  model_id: string;
  deployed_at: string | null;
  selected_at?: string | null;
  is_selected?: boolean;
  is_active?: boolean;
  error: string | null;
  created_at: string;
  artifact?: {
    path: string;
    file_count: number;
    tree: string;
    description?: string;
    file_entries?: Array<{ path: string; size: number; role: string; language: string }>;
    service?: { attack_path?: string; attack_parameter?: string; health_path?: string; port?: number };
    validation?: {
      status?: string;
      vulnerable_exploit_succeeded?: boolean;
      patched_exploit_failed?: boolean;
      checker_passed_before_patch?: boolean;
      checker_passed_after_patch?: boolean;
      runtime_validated?: boolean;
      contract_compatible?: boolean;
      deployable?: boolean;
      steps?: Array<{ name: string; status: string; output?: string; duration_ms?: number }>;
    };
  } | null;
}

interface AgentRun {
  id: string;
  team_id: number;
  bot_name: string;
  status: string;
  agent_type: string;
  provider: string;
  model_id: string;
  run_id: string;
  summary: { captures: number; accepted: number; failures: number; current_activity: { type: string } | null };
}

interface MatchAssignment {
  team_id: number;
  assignment_kind: 'attack_defense' | 'scripted';
  config: { bot_name?: string; planner?: string; provider?: string; model_id?: string; actions?: string[] };
  updated_at: string;
  latest_deployment?: AgentRun | null;
}

interface MatchPlan {
  assignments: MatchAssignment[];
  deployed_challenge: ChallengeRun | null;
  selected_challenge: ChallengeRun | null;
  latest_published_challenge: ChallengeRun | null;
  instructions: string[];
}

interface ActivityEntry {
  kind: string;
  summary: string;
  created_at: string;
  data?: Record<string, unknown>;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await botApiRequest(path, opts);
  const body = await r.json().catch(() => ({})) as T & { error?: string; output?: string };
  if (!r.ok) {
    const detail = body.error || body.output || `HTTP ${r.status}`;
    throw new Error(detail.length > 1200 ? `${detail.slice(0, 1200)}...` : detail);
  }
  return body;
}

function renderMd(md: string): string {
  return md
    .replace(/```[\w]*\n([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^---$/gm, '<hr/>')
    .replace(/^(?!<[h1-6p]|---)(.*\S.*)$/gm, '<p>$1</p>');
}

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);

// ── Sub-components ───────────────────────────────────────────────────────────

function VulnBadge({ v }: { v: string }) {
  return <span className={`challenge-vuln-badge challenge-vuln-badge--${v}`}>{v.replace(/_/g, ' ')}</span>;
}

function DiffBadge({ d }: { d: string }) {
  return <span className={`challenge-diff-badge challenge-diff-badge--${d}`}>{d}</span>;
}

function StatusPill({ s }: { s: string }) {
  return <span className={`challenge-status-pill challenge-status-pill--${s}`}>{s}</span>;
}

function mergeProviders(providers: Provider[]): Provider[] {
  const byId = new Map(DEFAULT_PROVIDERS.map(p => [p.id, p]));
  providers.forEach(p => byId.set(p.id, { ...byId.get(p.id), ...p, models: p.models?.length ? p.models : byId.get(p.id)?.models }));
  return [...byId.values()];
}

function ProviderModelFields({
  providers,
  provider,
  modelId,
  onProvider,
  onModel,
}: {
  providers: Provider[];
  provider: string;
  modelId: string;
  onProvider: (provider: string) => void;
  onModel: (modelId: string) => void;
}) {
  const options = mergeProviders(providers);
  const selected = options.find(p => p.id === provider) ?? options[0];
  return (
    <div className="form-row">
      <label className="drawer-field">
        <span>Provider</span>
        <select value={provider} onChange={e => { onProvider(e.target.value); onModel(''); }}>
          {options.map(p => (
            <option key={p.id} value={p.id}>{p.label}{p.available ? '' : ' (key not detected)'}</option>
          ))}
        </select>
      </label>
      <label className="drawer-field">
        <span>Model <small>(blank = provider default)</small></span>
        <select value={modelId} onChange={e => onModel(e.target.value)}>
          <option value="">Default ({selected?.models?.[0] ?? 'provider default'})</option>
          {(selected?.models ?? []).map(m => <option key={m} value={m}>{m}</option>)}
        </select>
      </label>
    </div>
  );
}

function LogDrawer({ title, runId, endpoint, onClose }: {
  title: string; runId: string; endpoint: string; onClose: () => void;
}) {
  const [md, setMd] = useState('');
  const [loading, setLoading] = useState(true);
  const [deploying, setDeploying] = useState(false);
  const [deployOut, setDeployOut] = useState('');
  const timerRef = useRef<ReturnType<typeof setInterval>>();

  const load = useCallback(() => {
    botApiRequest(`${endpoint}?limit=150`)
      .then(r => r.text()).then(t => { setMd(t); setLoading(false); }).catch(() => {});
  }, [endpoint]);

  useEffect(() => {
    load();
    timerRef.current = setInterval(load, 5000);
    return () => clearInterval(timerRef.current);
  }, [load]);

  async function deployToArena() {
    setDeploying(true);
    setDeployOut('Deploying challenge to every team and rebuilding app containers...');
    try {
      const r = await api<{ ok: boolean; output: string }>(`${endpoint.replace('/log', '/deploy')}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      setDeployOut(r.output || (r.ok ? 'Deployed.' : 'Failed.'));
    } catch (e) { setDeployOut(e instanceof Error ? e.message : String(e)); }
    setDeploying(false);
  }

  const isChallenge = endpoint.startsWith('/challenges/');
  const isDone = md.includes('published') || md.includes('PUBLISHED');

  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="ops-drawer ops-drawer--inspect" onMouseDown={e => e.stopPropagation()}>
        <div className="drawer-heading">
          <div><span className="section-icon"><ScrollText size={17}/></span><div><h2>{title}</h2><p>{runId}</p></div></div>
          <button className="icon-button" onClick={onClose}><X size={17}/></button>
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
          {loading
            ? <p style={{ color: 'var(--muted)' }}>Loading log…</p>
            : <div className="md-log" dangerouslySetInnerHTML={{ __html: renderMd(md) }}/>}
          {deployOut && <pre className="deploy-output-pre" style={{ marginTop: 12 }}>{deployOut}</pre>}
        </div>
        {isChallenge && isDone && (
          <div className="drawer-footer">
            <span style={{ color: 'var(--muted)', fontSize: 12 }}>Deploy copies this app to every team; Match controls then starts round 1.</span>
            <button className="primary-button" disabled={deploying} onClick={deployToArena}>
              <Rocket size={15}/>{deploying ? 'Deploying…' : 'Deploy to Arena'}
            </button>
          </div>
        )}
      </aside>
    </div>
  );
}

function ChallengeInspector({
  challenge,
  onClose,
  onChanged,
  onDelete,
}: {
  challenge: ChallengeRun;
  onClose: () => void;
  onChanged: (message: string) => void;
  onDelete: (challenge: ChallengeRun) => void;
}) {
  const [tab, setTab] = useState<'overview' | 'files' | 'activity'>('overview');
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [selectedFile, setSelectedFile] = useState(challenge.artifact?.file_entries?.[0]?.path || '');
  const [fileContent, setFileContent] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api<{ entries: ActivityEntry[] }>(`/challenges/${challenge.id}/activity?limit=100`)
      .then(body => setActivity(body.entries))
      .catch(() => setActivity([]));
  }, [challenge.id]);

  useEffect(() => {
    if (!selectedFile || tab !== 'files') return;
    setFileContent('Loading...');
    api<{ file: { content: string } }>(
      `/challenges/${challenge.id}/files?path=${encodeURIComponent(selectedFile)}`,
    )
      .then(body => setFileContent(body.file.content))
      .catch(err => setFileContent(err instanceof Error ? err.message : String(err)));
  }, [challenge.id, selectedFile, tab]);

  async function selectForMatch() {
    setBusy(true); setError('');
    try {
      await api(`/challenges/${challenge.id}/select`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      onChanged('Challenge selected for the next match.');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function deployNow() {
    setBusy(true); setError('');
    try {
      const result = await api<{ output?: string }>(`/challenges/${challenge.id}/deploy`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      onChanged(result.output || 'Challenge deployed and verified on every team.');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const validation = challenge.artifact?.validation;
  const runtimeVerified = Boolean(validation?.runtime_validated);
  const checks = [
    ['Checker before patch', validation?.checker_passed_before_patch],
    ['Exploit captures flag', validation?.vulnerable_exploit_succeeded],
    ['Checker after patch', validation?.checker_passed_after_patch],
    ['Exploit blocked by patch', validation?.patched_exploit_failed],
  ] as const;

  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="ops-drawer challenge-inspector" onMouseDown={event => event.stopPropagation()}>
        <div className="drawer-heading">
          <div>
            <span className="section-icon"><FlaskConical size={17}/></span>
            <div>
              <h2>{challenge.vulnerability.replace(/_/g, ' ')}</h2>
              <p>{challenge.challenge_id || challenge.id}</p>
            </div>
          </div>
          <button className="icon-button" onClick={onClose} title="Close"><X size={17}/></button>
        </div>

        <div className="inspector-state">
          <span className={challenge.is_active ? 'is-active' : ''}>
            <Activity size={14}/> Arena {challenge.is_active ? 'active' : 'not active'}
          </span>
          <span className={challenge.is_selected ? 'is-selected' : ''}>
            <Target size={14}/> {challenge.is_selected ? 'Selected for next match' : 'Not selected'}
          </span>
        </div>

        <div className="inspector-tabs" role="tablist">
          <button className={tab === 'overview' ? 'is-active' : ''} onClick={() => setTab('overview')}><ShieldCheck size={14}/>Overview</button>
          <button className={tab === 'files' ? 'is-active' : ''} onClick={() => setTab('files')}><FileCode2 size={14}/>Files</button>
          <button className={tab === 'activity' ? 'is-active' : ''} onClick={() => setTab('activity')}><ListTree size={14}/>Activity</button>
        </div>

        <div className="challenge-inspector__body">
          {tab === 'overview' && (
            <>
              <section className="challenge-overview">
                <p>{challenge.artifact?.description || 'Generated attack-defense service.'}</p>
                <dl>
                  <div><dt>Attack path</dt><dd><code>{challenge.artifact?.service?.attack_path || 'unknown'}</code></dd></div>
                  <div><dt>Input</dt><dd><code>{challenge.artifact?.service?.attack_parameter || 'unknown'}</code></dd></div>
                  <div><dt>Difficulty</dt><dd>{challenge.difficulty}</dd></div>
                  <div><dt>Seed</dt><dd>{challenge.seed}</dd></div>
                </dl>
              </section>
              <section className="validation-panel">
                <div className="validation-panel__heading">
                  <div><CheckCircle2 size={16}/><strong>Match readiness</strong></div>
                  <span className={`validation-state validation-state--${validation?.status || 'unknown'}`}>{validation?.status || 'unknown'}</span>
                </div>
                <div className="validation-grid">
                  {checks.map(([label, passed]) => (
                    <span className={passed && runtimeVerified ? 'is-passed' : 'is-failed'} key={label}>
                      <CheckCircle2 size={14}/>{label}<strong>{passed && runtimeVerified ? 'Passed' : 'Not verified'}</strong>
                    </span>
                  ))}
                </div>
                {!validation?.deployable && (
                  <div className="validation-warning">
                    This is a legacy or static-only artifact. Generate it again to receive live checker, exploit, and patch verification.
                  </div>
                )}
              </section>
            </>
          )}

          {tab === 'files' && (
            <div className="artifact-browser">
              <nav aria-label="Generated challenge files">
                {(challenge.artifact?.file_entries || []).map(file => (
                  <button
                    className={selectedFile === file.path ? 'is-active' : ''}
                    key={file.path}
                    onClick={() => setSelectedFile(file.path)}
                  >
                    <FileCode2 size={13}/>
                    <span><strong>{file.path}</strong><small>{file.role}</small></span>
                  </button>
                ))}
              </nav>
              <section>
                <header><strong>{selectedFile || 'Select a file'}</strong></header>
                <pre><code>{fileContent}</code></pre>
              </section>
            </div>
          )}

          {tab === 'activity' && (
            <div className="activity-timeline">
              {activity.map((entry, index) => (
                <article key={`${entry.created_at}-${index}`}>
                  <span className={`activity-dot activity-dot--${entry.kind}`}/>
                  <div>
                    <header><strong>{entry.kind.replace(/_/g, ' ')}</strong><time>{new Date(entry.created_at).toLocaleTimeString()}</time></header>
                    <p>{entry.summary || 'Completed.'}</p>
                  </div>
                </article>
              ))}
              {activity.length === 0 && <p className="empty-state">No generation activity has been recorded.</p>}
            </div>
          )}

          {error && <div className="inline-alert" role="alert">{error}</div>}
        </div>

        <div className="drawer-footer">
          <span>{challenge.artifact?.file_count || 0} generated files</span>
          <div className="drawer-footer__actions">
            <button disabled={busy || challenge.status !== 'published' || !validation?.deployable} onClick={selectForMatch}>
              <Target size={15}/>{challenge.is_selected ? 'Selected' : 'Use for next match'}
            </button>
            <button className="danger-button" disabled={busy} onClick={() => onDelete(challenge)}>
              <Trash2 size={15}/>Remove
            </button>
            <button className="primary-button" disabled={busy || challenge.status !== 'published' || !validation?.deployable} onClick={deployNow}>
              <Rocket size={15}/>{busy ? 'Working...' : challenge.is_active ? 'Redeploy and verify' : 'Deploy now'}
            </button>
          </div>
        </div>
      </aside>
    </div>
  );
}

// ── Challenge Lab ─────────────────────────────────────────────────────────────

function ChallengeLab({ providers }: { providers: Provider[] }) {
  const [challenges, setChallenges] = useState<ChallengeRun[]>([]);
  const [options, setOptions] = useState<ChallengeOptions | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [inspectId, setInspectId] = useState<string | null>(null);
  const [filter, setFilter] = useState<'active' | 'all'>('all');

  // Form state
  const [vuln, setVuln] = useState('path_traversal');
  const [diff, setDiff] = useState('easy');
  const [decoy, setDecoy] = useState(0);
  const [seed, setSeed] = useState<number | ''>('');
  const [provider, setProvider] = useState('fake');
  const [modelId, setModelId] = useState('');
  const [attempts, setAttempts] = useState(3);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');

  const load = useCallback(() => {
    api<{ challenges: ChallengeRun[] }>('/challenges')
      .then(d => setChallenges(d.challenges))
      .catch(error => setNotice(errorMessage(error)));
  }, []);

  useEffect(() => {
    load();
    api<ChallengeOptions>('/challenges/options')
      .then(setOptions)
      .catch(error => setNotice(errorMessage(error)));
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const visible = challenges.filter(
    c => filter === 'all' || c.status === 'running' || c.is_selected || c.is_active,
  );
  const inspectRun = challenges.find(c => c.id === inspectId);
  const selectedChallenge = challenges.find(c => c.is_selected);
  const activeChallenge = challenges.find(c => c.is_active);
  const vulnerabilityOptions = options?.vulnerabilities ?? [
    { id:'path_traversal', label:'Path Traversal', icon:'', description:'Directory traversal via /export' },
    { id:'sql_injection', label:'SQL Injection', icon:'', description:'Bypass login via SQLi' },
    { id:'command_injection', label:'Command Injection', icon:'', description:'OS command via diagnostics' },
  ];
  const difficultyOptions = options?.difficulties?.length ? options.difficulties : [
    { id: 'easy', label: 'Easy' },
    { id: 'medium', label: 'Medium' },
  ];

  async function generate() {
    setBusy(true); setNotice('');
    try {
      await api('/challenges/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vulnerability: vuln, difficulty: diff, decoy_endpoints: decoy,
          seed: seed === '' ? undefined : seed, provider, model_id: modelId || undefined, max_attempts: attempts }),
      });
      setNotice('Generation started — watch the log for progress.'); setCreateOpen(false); load();
    } catch (e) { setNotice(String(e)); }
    setBusy(false);
  }

  async function selectChallenge(id: string) {
    setBusy(true); setNotice('');
    try {
      await api(`/challenges/${id}/select`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      setNotice('Selected for the next match. Prepare Match will deploy this exact challenge.');
      load();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function deleteChallenge(challenge: ChallengeRun) {
    const warning = challenge.is_active
      ? 'This challenge is marked active in the arena. Removing it clears persistent Challenge Lab state, but it does not rebuild live team containers.'
      : challenge.is_selected
        ? 'This challenge is selected for the next match. Removing it clears that selection.'
        : 'Remove this generated vulnerable app and its stored activity?';
    const label = `${challenge.vulnerability.replace(/_/g, ' ')} seed ${challenge.seed}`;
    if (!window.confirm(`${warning}\n\nRemove ${label}?`)) return;

    setBusy(true); setNotice('');
    try {
      const result = await api<{ challenges: ChallengeRun[]; deleted?: { artifact_deleted?: boolean } }>(
        `/challenges/${challenge.id}`,
        { method: 'DELETE' },
      );
      setChallenges(result.challenges);
      setInspectId(current => current === challenge.id ? null : current);
      setNotice(result.deleted?.artifact_deleted
        ? 'Challenge removed from persistent state and published artifacts.'
        : 'Challenge removed from persistent state.');
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="challenge-lab-section">
      {/* Hero */}
      <section className="fleet-hero">
        <div>
          <span className="page-kicker">Challenge Lab</span>
          <h1>Vulnerability templates</h1>
          <p>Generate a unique vulnerable app, validate it, and inject it into all team containers before a round.</p>
        </div>
        <div className="fleet-hero__actions">
          <button className="icon-button" onClick={load}><RefreshCw size={16}/></button>
          <button className="primary-button" onClick={() => setCreateOpen(true)}><Plus size={16}/>Generate challenge</button>
        </div>
      </section>

      <section className="challenge-config-panel" aria-label="Challenge generator controls">
        <label className="drawer-field">
          <span>Exploit type</span>
          <select value={vuln} onChange={event => setVuln(event.target.value)}>
            {vulnerabilityOptions.map(option => (
              <option key={option.id} value={option.id}>{option.label}</option>
            ))}
          </select>
        </label>
        <label className="drawer-field">
          <span>Difficulty</span>
          <select value={diff} onChange={event => setDiff(event.target.value)}>
            {difficultyOptions.map(option => (
              <option key={option.id} value={option.id}>{option.label}</option>
            ))}
          </select>
        </label>
        <ProviderModelFields
          providers={providers}
          provider={provider}
          modelId={modelId}
          onProvider={setProvider}
          onModel={setModelId}
        />
        <button className="primary-button" disabled={busy} onClick={generate}>
          <Rocket size={15}/>{busy ? 'Starting...' : 'Create vuln app'}
        </button>
      </section>

      <section className="challenge-match-setup" aria-label="Challenge selected for the match">
        <div>
          <span className="setup-icon"><Target size={17}/></span>
          <p><span>Next match</span><strong>{selectedChallenge ? selectedChallenge.vulnerability.replace(/_/g, ' ') : 'No challenge selected'}</strong><small>{selectedChallenge ? `seed ${selectedChallenge.seed}` : 'Choose one published challenge below'}</small></p>
        </div>
        <div>
          <span className="setup-icon"><Activity size={17}/></span>
          <p><span>Active in arena</span><strong>{activeChallenge ? activeChallenge.vulnerability.replace(/_/g, ' ') : 'Default service'}</strong><small>{activeChallenge?.deployed_at ? `deployed ${new Date(activeChallenge.deployed_at).toLocaleTimeString()}` : 'not replaced yet'}</small></p>
        </div>
        <div className="challenge-match-setup__status">
          {selectedChallenge?.id === activeChallenge?.id
            ? <><CheckCircle2 size={16}/>Selected challenge is active</>
            : selectedChallenge
              ? <><Rocket size={16}/>Prepare Match will deploy and verify the selection</>
              : <><Target size={16}/>Selection required before match preparation</>}
        </div>
      </section>

      {/* Stats */}
      <section className="fleet-pulse">
        <div><span className="pulse-icon is-violet"><FlaskConical size={17}/></span><p><span>Total</span><strong>{challenges.length}</strong></p></div>
        <div><span className="pulse-icon is-green"><Activity size={17}/></span><p><span>Published</span><strong>{challenges.filter(c=>c.status==='published').length}</strong></p></div>
        <div><span className="pulse-icon is-amber"><Rocket size={17}/></span><p><span>Arena active</span><strong>{activeChallenge ? 1 : 0}</strong></p></div>
      </section>

      {/* Toolbar */}
      <div className="fleet-toolbar">
        <div className="view-switch">
          <button className={filter==='active'?'is-active':''} onClick={()=>setFilter('active')}>Active</button>
          <button className={filter==='all'?'is-active':''} onClick={()=>setFilter('all')}>History</button>
        </div>
        <span>{visible.length} challenge{visible.length!==1?'s':''}</span>
      </div>

      {/* Cards */}
      <section className="deployment-grid">
        {visible.map(c => (
          <article
            key={c.id}
            className={`deployment-card challenge-card ${c.is_active ? 'is-arena-active' : ''} ${c.is_selected ? 'is-match-selected' : ''} status-${c.status === 'published' ? 'running' : c.status === 'failed' ? 'failed' : 'deploying'}`}
          >
            <div className="deployment-card__top">
              <span className="team-orb"><FlaskConical size={14}/></span>
              <div>
                <strong>{c.vulnerability.replace(/_/g, ' ')}</strong>
                <span>seed {c.seed} · {c.provider}{c.model_id ? ` / ${c.model_id}` : ''}</span>
              </div>
              <StatusPill s={c.is_active ? 'active' : c.is_selected ? 'selected' : c.status === 'published' ? 'ready' : c.status}/>
            </div>
            <div className="deployment-activity">
              <Activity size={14}/>
              <span>{c.is_active
                ? 'Currently installed on every team'
                : c.is_selected
                  ? 'Will be installed during Match preparation'
                  : c.status === 'running'
                    ? 'Generating and validating...'
                    : c.error || c.artifact?.description || 'Ready to inspect and select'}</span>
            </div>
            <div className="deployment-stats">
              <span><VulnBadge v={c.vulnerability}/></span>
              <span><DiffBadge d={c.difficulty}/></span>
              {c.artifact && <span><b>{c.artifact.file_count}</b> files</span>}
              {c.decoy_endpoints > 0 && <span><b>{c.decoy_endpoints}</b> decoys</span>}
            </div>
            <div className="challenge-card__actions">
              <button onClick={() => setInspectId(c.id)}><FileCode2 size={14}/>Inspect</button>
              <button
                className={c.is_selected ? 'is-selected' : ''}
                disabled={busy || c.status !== 'published' || c.is_selected || !c.artifact?.validation?.deployable}
                onClick={() => void selectChallenge(c.id)}
              >
                <Target size={14}/>{c.is_selected ? 'Selected for match' : 'Use for next match'}
              </button>
              <button className="danger-button" disabled={busy || c.status === 'running'} onClick={() => void deleteChallenge(c)}>
                <Trash2 size={14}/>Remove
              </button>
            </div>
          </article>
        ))}
        {visible.length === 0 && (
          <div className="fleet-empty">
            <FlaskConical size={26}/>
            <h2>No challenges yet</h2>
            <p>Generate a unique vulnerable app to use for the next round.</p>
            <button className="primary-button" onClick={() => setCreateOpen(true)}><Plus size={15}/>Generate challenge</button>
          </div>
        )}
      </section>

      {notice && <div className="challenge-notice" role="status" aria-live="polite">{notice}</div>}

      {/* Create drawer */}
      {createOpen && (
        <div className="drawer-backdrop" onMouseDown={() => setCreateOpen(false)}>
          <aside className="ops-drawer ops-drawer--wide" onMouseDown={e => e.stopPropagation()}>
            <div className="drawer-heading">
              <div><span className="section-icon"><FlaskConical size={17}/></span><div><h2>Generate challenge</h2><p>Creates a unique vulnerable Flask app + checker + exploit.</p></div></div>
              <button className="icon-button" onClick={() => setCreateOpen(false)}><X size={17}/></button>
            </div>
            <div className="deployment-form">
              {/* Vulnerability */}
              <fieldset className="choice-fieldset">
                <legend>Vulnerability type</legend>
                <div className="choice-grid">
                  {vulnerabilityOptions.map(v => (
                    <label key={v.id} className={vuln === v.id ? 'is-selected' : ''} title={v.description}>
                      <input type="radio" name="vuln" checked={vuln===v.id} onChange={() => setVuln(v.id)}/>
                      {v.icon ? `${v.icon} ` : ''}{v.label}
                    </label>
                  ))}
                </div>
              </fieldset>

              {/* Difficulty + decoys */}
              <div className="form-row">
                <fieldset className="choice-fieldset">
                  <legend>Difficulty</legend>
                  <div className="choice-grid">
                    {difficultyOptions.map(d => (
                      <label key={d.id} className={diff===d.id?'is-selected':''}>
                        <input type="radio" name="diff" checked={diff===d.id} onChange={() => setDiff(d.id)}/>{d.label}
                      </label>
                    ))}
                  </div>
                </fieldset>
                <fieldset className="choice-fieldset">
                  <legend>Decoy endpoints (0–3)</legend>
                  <div className="choice-grid">
                    {[0,1,2,3].map(n => (
                      <label key={n} className={decoy===n?'is-selected':''}>
                        <input type="radio" name="decoy" checked={decoy===n} onChange={() => setDecoy(n)}/>{n}
                      </label>
                    ))}
                  </div>
                </fieldset>
              </div>

              {/* Seed + attempts */}
              <div className="form-row">
                <label className="drawer-field">
                  <span>Seed <small>(blank = random)</small></span>
                  <input type="number" placeholder="random" value={seed}
                    onChange={e => setSeed(e.target.value === '' ? '' : Number(e.target.value))}/>
                </label>
                <label className="drawer-field">
                  <span>Max attempts</span>
                  <input type="number" min={1} max={5} value={attempts} onChange={e => setAttempts(Number(e.target.value))}/>
                </label>
              </div>

              <ProviderModelFields
                providers={providers}
                provider={provider}
                modelId={modelId}
                onProvider={setProvider}
                onModel={setModelId}
              />
            </div>
            <div className="drawer-footer">
              <span><VulnBadge v={vuln}/> <DiffBadge d={diff}/></span>
              <button className="primary-button" disabled={busy} onClick={generate}>
                <Rocket size={15}/>{busy ? 'Starting…' : 'Generate'}
              </button>
            </div>
          </aside>
        </div>
      )}

      {/* Challenge inspector */}
      {inspectId && inspectRun && (
        <ChallengeInspector
          challenge={inspectRun}
          onClose={() => setInspectId(null)}
          onChanged={message => { setNotice(message); load(); }}
          onDelete={challenge => void deleteChallenge(challenge)}
        />
      )}
    </div>
  );
}

// ── AI Agents section ─────────────────────────────────────────────────────────

function AgentsSection({ providers }: { providers: Provider[] }) {
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [plan, setPlan] = useState<MatchPlan | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [inspectId, setInspectId] = useState<string | null>(null);
  const [teams, setTeams] = useState<{ id: number; container_up: boolean }[]>([]);
  const [selectedTeams, setSelectedTeams] = useState<Set<number>>(new Set());
  const [kind, setKind] = useState<'attack_defense' | 'scripted'>('attack_defense');
  const [provider, setProvider] = useState('fake');
  const [modelId, setModelId] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [filter, setFilter] = useState<'active' | 'all'>('active');

  const load = useCallback(() => {
    api<{ agent_runs: AgentRun[] }>('/agent-runs?agent_type=attack_defense')
      .then(d => setRuns(d.agent_runs)).catch(error => setNotice(errorMessage(error)));
    api<{ teams: { id: number; container_up: boolean }[] }>('/status')
      .then(d => setTeams(d.teams)).catch(error => setNotice(errorMessage(error)));
    api<MatchPlan>('/match-plan')
      .then(setPlan).catch(error => setNotice(errorMessage(error)));
  }, []);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  const visible = runs.filter(r => filter === 'all' || ['RUNNING','DEPLOYING'].includes(r.status));
  const inspectRun = runs.find(r => r.id === inspectId);

  function toggleTeam(id: number) {
    setSelectedTeams(prev => { const s = new Set(prev); s.has(id) ? s.delete(id) : s.add(id); return s; });
  }

  async function assign() {
    setBusy(true); setNotice('');
    try {
      const actions = kind === 'attack_defense'
        ? ['attack.recon','attack.exploit','defend.inspect_files','defend.run_checker','defend.apply_patch','defend.run_exploit_regression']
        : ['recon.health','exploit.path_traversal','exploit.cmdi','exploit.sqli'];
      await api('/match-plan/agents', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          teams: [...selectedTeams], assignment_kind: kind,
          planner: kind === 'attack_defense' ? 'model' : 'recon_first',
          bot_name: `${kind === 'attack_defense' ? 'AttackDefenseAgent' : 'Scripted bot'} (${[...selectedTeams].map(t=>`T${t}`).join(',')})`,
          provider, model_id: modelId || undefined, actions,
          target_policy: 'all_opponents', target_teams: [],
          loop_interval: 30, stop_on_success: false,
          flag_re: 'FLAG\\{[a-f0-9]{32}\\}', timeout: 10,
        }),
      });
      setNotice('Assignment saved. Start the match from Match controls to launch queued agents.'); setCreateOpen(false); setSelectedTeams(new Set()); load();
    } catch (e) { setNotice(String(e)); }
    setBusy(false);
  }

  async function startAssignedAgents() {
    setBusy(true); setNotice('Preparing arena and starting assigned bots and agents…');
    try {
      const result = await api<{ deployments: AgentRun[]; output?: string; challenge_deployed?: boolean }>('/match-plan/prepare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deploy_selected_challenge: true, start_agents: true }),
      });
      setNotice(`${result.deployments.length} assigned runtime${result.deployments.length===1?'':'s'} started${result.challenge_deployed ? ' after arena regeneration' : ''}.`);
      load();
    } catch (e) { setNotice(e instanceof Error ? e.message : String(e)); }
    setBusy(false);
  }

  async function stopRun(id: string) {
    await api(`/deployments/${id}/stop`, { method: 'POST', headers: {'Content-Type':'application/json'}, body:'{}' });
    load();
  }

  return (
    <div>
      <div className="section-divider"><Swords size={14}/>AI Agents</div>

      <section className="fleet-hero" style={{ marginBottom: 0 }}>
        <div>
          <span className="page-kicker">Match plan</span>
          <p>Assign scripted bots or AI attack/defense agents before the match; Match controls prepares the arena and launches them before round 1.</p>
        </div>
        <div className="fleet-hero__actions">
          <button className="icon-button" onClick={load}><RefreshCw size={16}/></button>
          <button className="primary-button" disabled={busy || !plan?.assignments.length} onClick={() => void startAssignedAgents()}><Rocket size={16}/>Prepare and start</button>
          <button className="primary-button" onClick={() => setCreateOpen(true)}><Plus size={16}/>Assign team</button>
        </div>
      </section>

      <section className="challenge-config-panel agent-config-panel" aria-label="Agent model controls">
        <ProviderModelFields
          providers={providers}
          provider={provider}
          modelId={modelId}
          onProvider={setProvider}
          onModel={setModelId}
        />
        <button className="primary-button" onClick={() => setCreateOpen(true)}>
          <Plus size={15}/>Assign team with model
        </button>
      </section>

      <section className="fleet-pulse" style={{ marginTop: 14 }}>
        <div><span className="pulse-icon is-violet"><FlaskConical size={17}/></span><p><span>Selected challenge</span><strong>{plan?.selected_challenge ? 'Ready' : 'Required'}</strong><small>{plan?.selected_challenge?.vulnerability?.replace(/_/g, ' ') || 'Select one in Challenge Lab'}</small></p></div>
        <div><span className="pulse-icon is-blue"><Swords size={17}/></span><p><span>Assigned teams</span><strong>{plan?.assignments.length ?? 0}</strong><small>queued for match start</small></p></div>
      </section>

      <section className="deployment-grid" style={{ marginBottom: 18 }}>
        {plan?.assignments.map(a => (
          <article key={a.team_id} className="deployment-card status-deploying">
            <div className="deployment-card__top">
              <span className="team-orb">T{a.team_id}</span>
              <div><strong>{a.assignment_kind === 'attack_defense' ? 'AttackDefenseAgent' : 'Scripted bot'}</strong><span>{a.config.provider || a.config.planner || 'scripted'}{a.config.model_id ? ` / ${a.config.model_id}` : ''}</span></div>
              <span className="deployment-status">{a.latest_deployment?.status || 'QUEUED'}</span>
            </div>
            <div className="deployment-activity"><Activity size={14}/><span>Will launch from the same prepare flow used by Match controls.</span></div>
            <div className="deployment-stats"><span><b>{a.config.actions?.length ?? 0}</b> actions</span><span>{new Date(a.updated_at).toLocaleTimeString()}</span></div>
          </article>
        ))}
      </section>

      <div className="fleet-toolbar" style={{ marginTop: 14 }}>
        <div className="view-switch">
          <button className={filter==='active'?'is-active':''} onClick={()=>setFilter('active')}>Active</button>
          <button className={filter==='all'?'is-active':''} onClick={()=>setFilter('all')}>History</button>
        </div>
        <span>{visible.length} agent{visible.length!==1?'s':''}</span>
      </div>

      <section className="deployment-grid">
        {visible.map(r => (
          <button key={r.id} className={`deployment-card status-${r.status.toLowerCase()}`} onClick={() => setInspectId(r.id)}>
            <div className="deployment-card__top">
              <span className="team-orb">T{r.team_id}</span>
              <div><strong>{r.bot_name}</strong><span>{r.provider}{r.model_id?` / ${r.model_id}`:''}</span></div>
              <span className="deployment-status">{r.status}</span>
            </div>
            <div className="deployment-activity">
              <Activity size={14}/>
              <span>{r.summary?.current_activity?.type ?? 'Waiting for telemetry'}</span>
            </div>
            <div className="deployment-stats">
              <span><b>{r.summary?.captures??0}</b> captures</span>
              <span><b>{r.summary?.accepted??0}</b> accepted</span>
              <span><b>{r.summary?.failures??0}</b> errors</span>
            </div>
          </button>
        ))}
        {visible.length === 0 && (
          <div className="fleet-empty">
            <Brain size={26}/>
            <h2>No agents {filter==='active'?'running':'deployed'}</h2>
            <p>Assign a team above, then start the match with queued agents enabled.</p>
            <button className="primary-button" onClick={() => setCreateOpen(true)}><Plus size={15}/>Assign team</button>
          </div>
        )}
      </section>

      {notice && <div className="sr-status" role="status" aria-live="polite">{notice}</div>}

      {/* Create drawer */}
      {createOpen && (
        <div className="drawer-backdrop" onMouseDown={() => setCreateOpen(false)}>
          <aside className="ops-drawer ops-drawer--wide" onMouseDown={e => e.stopPropagation()}>
            <div className="drawer-heading">
              <div><span className="section-icon"><Brain size={17}/></span><div><h2>Assign for match start</h2><p>Queued assignments launch when the match starts.</p></div></div>
              <button className="icon-button" onClick={() => setCreateOpen(false)}><X size={17}/></button>
            </div>
            <div className="deployment-form">
              <fieldset className="choice-fieldset">
                <legend>Runtime type</legend>
                <div className="choice-grid">
                  <label className={kind==='attack_defense'?'is-selected':''}>
                    <input type="radio" checked={kind==='attack_defense'} onChange={()=>setKind('attack_defense')}/>Attack / Defense Agent
                  </label>
                  <label className={kind==='scripted'?'is-selected':''}>
                    <input type="radio" checked={kind==='scripted'} onChange={()=>setKind('scripted')}/>Scripted Bot
                  </label>
                </div>
              </fieldset>

              <fieldset className="choice-fieldset">
                <legend><Swords size={14}/>Deploy as teams</legend>
                <div className="choice-grid">
                  {teams.map(t => (
                    <label key={t.id} className={`${selectedTeams.has(t.id)?'is-selected':''} ${!t.container_up?'is-disabled':''}`}>
                      <input type="checkbox" checked={selectedTeams.has(t.id)} disabled={!t.container_up} onChange={()=>toggleTeam(t.id)}/>
                      <span className="team-orb">T{t.id}</span> team{t.id}
                      <small>{t.container_up?'Ready':'Offline'}</small>
                    </label>
                  ))}
                </div>
              </fieldset>

              <ProviderModelFields
                providers={providers}
                provider={provider}
                modelId={modelId}
                onProvider={setProvider}
                onModel={setModelId}
              />
            </div>
            <div className="drawer-footer">
              <span>{selectedTeams.size} team{selectedTeams.size!==1?'s':''} selected</span>
              <button className="primary-button" disabled={busy||selectedTeams.size===0} onClick={assign}>
                <Rocket size={15}/>{busy?'Saving…':'Save assignment'}
              </button>
            </div>
          </aside>
        </div>
      )}

      {/* Log inspect drawer */}
      {inspectId && inspectRun && (
        <LogDrawer
          title={inspectRun.bot_name}
          runId={inspectId}
          endpoint={`/agent-runs/${inspectId}/log`}
          onClose={() => setInspectId(null)}
        />
      )}

      {/* Stop running agents */}
      {inspectId && inspectRun && ['RUNNING','DEPLOYING'].includes(inspectRun.status) && (
        <div style={{ position:'fixed', bottom:24, right:24, zIndex:9999 }}>
          <button className="danger-button" onClick={() => { void stopRun(inspectId); setInspectId(null); }}>
            <CircleStop size={14}/>Stop agent
          </button>
        </div>
      )}
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

export default function AgentsPanel() {
  const [providers, setProviders] = useState<Provider[]>(DEFAULT_PROVIDERS);

  useEffect(() => {
    api<{ providers: Provider[] }>('/providers')
      .then(d => setProviders(d.providers)).catch(() => {});
  }, []);

  return (
    <main className="fleet-page">
      <ChallengeLab providers={providers}/>
      <AgentsSection providers={providers}/>
    </main>
  );
}
