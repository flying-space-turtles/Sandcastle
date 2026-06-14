import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity, Brain, ChevronDown, CircleStop, FlaskConical,
  Plus, RefreshCw, Rocket, ScrollText, Swords, X,
} from 'lucide-react';
import { botApiUrl } from '../data/arenaConfig';

// ── Types ────────────────────────────────────────────────────────────────────

interface Provider { id: string; label: string; available: boolean; models?: string[]; }

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
  error: string | null;
  created_at: string;
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

// ── Helpers ──────────────────────────────────────────────────────────────────

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(`${botApiUrl}${path}`, opts);
  const body = await r.json().catch(() => ({})) as T & { error?: string };
  if (!r.ok) throw new Error((body as any).error || `HTTP ${r.status}`);
  return body;
}

function renderMd(md: string): string {
  return md
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/```[\w]*\n([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
    .replace(/^---$/gm, '<hr/>')
    .replace(/^(?!<[h1-6p]|---)(.*\S.*)$/gm, '<p>$1</p>');
}

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

function LogDrawer({ title, runId, endpoint, onClose }: {
  title: string; runId: string; endpoint: string; onClose: () => void;
}) {
  const [md, setMd] = useState('');
  const [loading, setLoading] = useState(true);
  const [deploying, setDeploying] = useState(false);
  const [deployOut, setDeployOut] = useState('');
  const timerRef = useRef<ReturnType<typeof setInterval>>();

  const load = useCallback(() => {
    fetch(`${botApiUrl}${endpoint}?limit=150`)
      .then(r => r.text()).then(t => { setMd(t); setLoading(false); }).catch(() => {});
  }, [endpoint]);

  useEffect(() => {
    load();
    timerRef.current = setInterval(load, 5000);
    return () => clearInterval(timerRef.current);
  }, [load]);

  async function deployToArena() {
    setDeploying(true);
    try {
      const r = await api<{ ok: boolean; output: string }>(`${endpoint.replace('/log', '/deploy')}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      setDeployOut(r.output || (r.ok ? 'Deployed.' : 'Failed.'));
    } catch (e) { setDeployOut(String(e)); }
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
            <span style={{ color: 'var(--muted)', fontSize: 12 }}>Ready to deploy to all team containers</span>
            <button className="primary-button" disabled={deploying} onClick={deployToArena}>
              <Rocket size={15}/>{deploying ? 'Deploying…' : 'Deploy to Arena'}
            </button>
          </div>
        )}
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
    api<{ challenges: ChallengeRun[] }>('/challenges').then(d => setChallenges(d.challenges)).catch(() => {});
  }, []);

  useEffect(() => {
    load();
    api<ChallengeOptions>('/challenges/options').then(setOptions).catch(() => {});
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const selectedProv = providers.find(p => p.id === provider);
  const visible = challenges.filter(c => filter === 'all' || c.status === 'running');
  const inspectRun = challenges.find(c => c.id === inspectId);

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

      {/* Stats */}
      <section className="fleet-pulse">
        <div><span className="pulse-icon is-violet"><FlaskConical size={17}/></span><p><span>Total</span><strong>{challenges.length}</strong></p></div>
        <div><span className="pulse-icon is-green"><Activity size={17}/></span><p><span>Published</span><strong>{challenges.filter(c=>c.status==='published').length}</strong></p></div>
        <div><span className="pulse-icon is-amber"><Rocket size={17}/></span><p><span>Deployed</span><strong>{challenges.filter(c=>c.deployed_at).length}</strong></p></div>
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
          <button
            key={c.id}
            className={`deployment-card status-${c.status === 'published' ? 'running' : c.status === 'failed' ? 'failed' : 'deploying'}`}
            onClick={() => setInspectId(c.id)}
          >
            <div className="deployment-card__top">
              <span className="team-orb"><FlaskConical size={14}/></span>
              <div>
                <strong>{c.vulnerability.replace(/_/g, ' ')}</strong>
                <span>seed {c.seed} · {c.provider}{c.model_id ? ` / ${c.model_id}` : ''}</span>
              </div>
              <StatusPill s={c.deployed_at ? 'deployed' : c.status}/>
            </div>
            <div className="deployment-activity">
              <Activity size={14}/>
              <span>{c.deployed_at ? `Deployed ${new Date(c.deployed_at).toLocaleTimeString()}` : c.status === 'running' ? 'Generating…' : c.error || c.status}</span>
            </div>
            <div className="deployment-stats">
              <span><VulnBadge v={c.vulnerability}/></span>
              <span><DiffBadge d={c.difficulty}/></span>
              {c.decoy_endpoints > 0 && <span><b>{c.decoy_endpoints}</b> decoys</span>}
            </div>
          </button>
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

      {notice && <div className="sr-status" role="status" aria-live="polite">{notice}</div>}

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
                  {(options?.vulnerabilities ?? [
                    { id:'path_traversal', label:'Path Traversal', icon:'🗂️', description:'Directory traversal via /export' },
                    { id:'sql_injection', label:'SQL Injection', icon:'🗄️', description:'Bypass login via SQLi' },
                    { id:'command_injection', label:'Command Injection', icon:'💻', description:'OS command via diagnostics' },
                  ]).map(v => (
                    <label key={v.id} className={vuln === v.id ? 'is-selected' : ''} title={v.description}>
                      <input type="radio" name="vuln" checked={vuln===v.id} onChange={() => setVuln(v.id)}/>
                      {v.icon} {v.label}
                    </label>
                  ))}
                </div>
              </fieldset>

              {/* Difficulty + decoys */}
              <div className="form-row">
                <fieldset className="choice-fieldset">
                  <legend>Difficulty</legend>
                  <div className="choice-grid">
                    {['easy','medium'].map(d => (
                      <label key={d} className={diff===d?'is-selected':''}>
                        <input type="radio" name="diff" checked={diff===d} onChange={() => setDiff(d)}/>{d}
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

              {/* Provider + model */}
              <div className="form-row">
                <label className="drawer-field">
                  <span>Provider</span>
                  <select value={provider} onChange={e => { setProvider(e.target.value); setModelId(''); }}>
                    {providers.map(p => (
                      <option key={p.id} value={p.id} disabled={!p.available}>{p.available ? '' : '🔒 '}{p.label}</option>
                    ))}
                  </select>
                </label>
                <label className="drawer-field">
                  <span>Model</span>
                  <select value={modelId} onChange={e => setModelId(e.target.value)} disabled={!selectedProv?.models?.length}>
                    {(selectedProv?.models ?? []).map(m => <option key={m} value={m}>{m}</option>)}
                    {!selectedProv?.models?.length && <option value="">— default —</option>}
                  </select>
                </label>
              </div>
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

      {/* Log / deploy drawer */}
      {inspectId && inspectRun && (
        <LogDrawer
          title={`${inspectRun.vulnerability.replace(/_/g,' ')} · ${inspectRun.difficulty}`}
          runId={inspectId}
          endpoint={`/challenges/${inspectId}/log`}
          onClose={() => setInspectId(null)}
        />
      )}
    </div>
  );
}

// ── AI Agents section ─────────────────────────────────────────────────────────

function AgentsSection({ providers }: { providers: Provider[] }) {
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [inspectId, setInspectId] = useState<string | null>(null);
  const [teams, setTeams] = useState<{ id: number; container_up: boolean }[]>([]);
  const [selectedTeams, setSelectedTeams] = useState<Set<number>>(new Set());
  const [kind, setKind] = useState<'attack_defense' | 'challenge_generator'>('attack_defense');
  const [provider, setProvider] = useState('fake');
  const [modelId, setModelId] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [filter, setFilter] = useState<'active' | 'all'>('active');

  const load = useCallback(() => {
    api<{ agent_runs: AgentRun[] }>('/agent-runs?agent_type=attack_defense')
      .then(d => setRuns(d.agent_runs)).catch(() => {});
    api<{ teams: { id: number; container_up: boolean }[] }>('/status')
      .then(d => setTeams(d.teams)).catch(() => {});
  }, []);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  const selectedProv = providers.find(p => p.id === provider);
  const visible = runs.filter(r => filter === 'all' || ['RUNNING','DEPLOYING'].includes(r.status));
  const inspectRun = runs.find(r => r.id === inspectId);

  function toggleTeam(id: number) {
    setSelectedTeams(prev => { const s = new Set(prev); s.has(id) ? s.delete(id) : s.add(id); return s; });
  }

  async function deploy() {
    setBusy(true); setNotice('');
    try {
      const actions = kind === 'attack_defense' ? [
        'attack.recon','attack.exploit','attack.submit_flag',
        'defend.inspect_files','defend.snapshot','defend.apply_patch',
        'defend.run_checker','defend.run_exploit_regression',
      ] : [];
      await api('/deployments', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          teams: [...selectedTeams], planner: 'model', agent_type: kind,
          bot_name: `AI-${kind} (${[...selectedTeams].map(t=>`T${t}`).join(',')})`,
          provider, model_id: modelId || undefined, actions,
          target_policy: 'all_opponents', target_teams: [],
          loop_interval: 30, stop_on_success: false,
          flag_re: 'FLAG\\{[a-f0-9]{32}\\}', timeout: 10,
        }),
      });
      setNotice('Agent deployed.'); setCreateOpen(false); setSelectedTeams(new Set()); load();
    } catch (e) { setNotice(String(e)); }
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
          <span className="page-kicker">Attack / Defense</span>
          <p>Deploy AI agents per team — they attack opponents and defend their service.</p>
        </div>
        <div className="fleet-hero__actions">
          <button className="icon-button" onClick={load}><RefreshCw size={16}/></button>
          <button className="primary-button" onClick={() => setCreateOpen(true)}><Plus size={16}/>Deploy agent</button>
        </div>
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
            <p>Deploy an AI agent to a team to begin autonomous attack/defense.</p>
            <button className="primary-button" onClick={() => setCreateOpen(true)}><Plus size={15}/>Deploy agent</button>
          </div>
        )}
      </section>

      {notice && <div className="sr-status" role="status" aria-live="polite">{notice}</div>}

      {/* Create drawer */}
      {createOpen && (
        <div className="drawer-backdrop" onMouseDown={() => setCreateOpen(false)}>
          <aside className="ops-drawer ops-drawer--wide" onMouseDown={e => e.stopPropagation()}>
            <div className="drawer-heading">
              <div><span className="section-icon"><Brain size={17}/></span><div><h2>Deploy AI agent</h2><p>One agent per team, per type.</p></div></div>
              <button className="icon-button" onClick={() => setCreateOpen(false)}><X size={17}/></button>
            </div>
            <div className="deployment-form">
              <fieldset className="choice-fieldset">
                <legend>Agent type</legend>
                <div className="choice-grid">
                  <label className={kind==='attack_defense'?'is-selected':''}>
                    <input type="radio" checked={kind==='attack_defense'} onChange={()=>setKind('attack_defense')}/>⚔️ Attack / Defense
                  </label>
                  <label className={kind==='challenge_generator'?'is-selected':''}>
                    <input type="radio" checked={kind==='challenge_generator'} onChange={()=>setKind('challenge_generator')} disabled/>🏗️ Challenge Generator (use Challenge Lab)
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

              <div className="form-row">
                <label className="drawer-field">
                  <span>Provider</span>
                  <select value={provider} onChange={e=>{setProvider(e.target.value);setModelId('');}}>
                    {providers.map(p=>(
                      <option key={p.id} value={p.id} disabled={!p.available}>{p.available?'':'🔒 '}{p.label}</option>
                    ))}
                  </select>
                </label>
                <label className="drawer-field">
                  <span>Model</span>
                  <select value={modelId} onChange={e=>setModelId(e.target.value)} disabled={!selectedProv?.models?.length}>
                    {(selectedProv?.models??[]).map(m=><option key={m} value={m}>{m}</option>)}
                    {!selectedProv?.models?.length && <option value="">— default —</option>}
                  </select>
                </label>
              </div>
            </div>
            <div className="drawer-footer">
              <span>{selectedTeams.size} team{selectedTeams.size!==1?'s':''} selected</span>
              <button className="primary-button" disabled={busy||selectedTeams.size===0} onClick={deploy}>
                <Rocket size={15}/>{busy?'Deploying…':'Deploy'}
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
  const [providers, setProviders] = useState<Provider[]>([
    { id:'fake', label:'Fake (no cost)', available:true },
  ]);

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
