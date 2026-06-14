import React, { useEffect, useRef, useState, useCallback } from 'react';

const BOT_API = `http://${window.location.hostname}:7000`;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Provider {
  id: string;
  label: string;
  available: boolean;
  models?: string[];
}

interface AgentRun {
  id: string;
  team_id: number;
  bot_name: string;
  status: string;
  agent_type: string;
  agent_id: string;
  run_id: string;
  provider: string;
  model_id: string;
  created_at: string;
  updated_at: string;
  summary?: {
    captures: number;
    submissions: number;
    accepted: number;
    failures: number;
    current_activity?: { type: string; [k: string]: unknown } | null;
  };
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

function useProviders() {
  const [providers, setProviders] = useState<Provider[]>([]);
  useEffect(() => {
    fetch(`${BOT_API}/providers`)
      .then(r => r.json())
      .then(d => setProviders(d.providers || []))
      .catch(() => {});
  }, []);
  return providers;
}

function useAgentRuns(type?: string, poll = 4000) {
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const fetchRuns = useCallback(() => {
    const url = type ? `${BOT_API}/agent-runs?agent_type=${type}` : `${BOT_API}/agent-runs`;
    fetch(url)
      .then(r => r.json())
      .then(d => setRuns(d.agent_runs || []))
      .catch(() => {});
  }, [type]);

  useEffect(() => {
    fetchRuns();
    const id = setInterval(fetchRuns, poll);
    return () => clearInterval(id);
  }, [fetchRuns, poll]);

  return { runs, refresh: fetchRuns };
}

// ---------------------------------------------------------------------------
// Markdown renderer (inline, zero deps)
// ---------------------------------------------------------------------------

function renderMarkdown(md: string): string {
  return md
    // h2
    .replace(/^## (.+)$/gm, '<h3 class="ag-md-h2">$1</h3>')
    // h1
    .replace(/^# (.+)$/gm, '<h2 class="ag-md-h1">$1</h2>')
    // bold
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // inline code
    .replace(/`([^`]+)`/g, '<code class="ag-md-code">$1</code>')
    // code fence
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="ag-md-pre"><code>$2</code></pre>')
    // hr
    .replace(/^---$/gm, '<hr class="ag-md-hr" />')
    // paragraph
    .replace(/^(?!<[hpc]|---)(.*\S.*)$/gm, '<p class="ag-md-p">$1</p>')
    // emojis pass through as-is
    ;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    RUNNING: '#22c55e',
    DEPLOYING: '#f59e0b',
    STOPPED: '#6b7280',
    FAILED: '#ef4444',
    SUPERSEDED: '#8b5cf6',
  };
  const color = colors[status] || '#6b7280';
  return (
    <span style={{
      background: color + '22',
      color,
      border: `1px solid ${color}55`,
      borderRadius: 6,
      padding: '2px 10px',
      fontSize: 11,
      fontWeight: 700,
      letterSpacing: '0.05em',
      textTransform: 'uppercase' as const,
    }}>{status}</span>
  );
}

function AgentTypeBadge({ type }: { type: string }) {
  const map: Record<string, { label: string; color: string }> = {
    attack_defense: { label: '⚔️ Attack/Defense', color: '#f97316' },
    challenge_generator: { label: '🏗️ Challenge Gen', color: '#a78bfa' },
    scripted: { label: '🤖 Scripted', color: '#38bdf8' },
  };
  const t = map[type] || { label: type, color: '#6b7280' };
  return (
    <span style={{
      background: t.color + '18',
      color: t.color,
      border: `1px solid ${t.color}44`,
      borderRadius: 6,
      padding: '2px 10px',
      fontSize: 11,
      fontWeight: 600,
    }}>{t.label}</span>
  );
}

function AgentCard({ run, onView, onStop }: {
  run: AgentRun;
  onView: (run: AgentRun) => void;
  onStop: (run: AgentRun) => void;
}) {
  const isActive = run.status === 'RUNNING' || run.status === 'DEPLOYING';
  return (
    <div style={{
      background: 'rgba(255,255,255,0.04)',
      border: '1px solid rgba(255,255,255,0.10)',
      borderRadius: 12,
      padding: '16px 20px',
      marginBottom: 12,
      transition: 'box-shadow 0.2s',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
        <AgentTypeBadge type={run.agent_type} />
        <StatusBadge status={run.status} />
        <span style={{ marginLeft: 'auto', fontSize: 12, color: '#6b7280' }}>
          Team {run.team_id}
        </span>
      </div>

      <div style={{ fontSize: 14, fontWeight: 600, color: '#f8fafc', marginBottom: 4 }}>
        {run.bot_name || run.agent_id}
      </div>
      <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 8 }}>
        <code style={{ color: '#7dd3fc' }}>{run.provider}</code>
        {run.model_id ? <> · <code style={{ color: '#86efac' }}>{run.model_id}</code></> : null}
        <span style={{ marginLeft: 8 }}>· run <code style={{ color: '#c4b5fd' }}>{run.run_id.slice(0, 8)}</code></span>
      </div>

      {run.summary && (
        <div style={{
          display: 'flex', gap: 16, fontSize: 12, color: '#94a3b8', marginBottom: 10,
          flexWrap: 'wrap' as const,
        }}>
          <span>🚩 <strong style={{ color: '#f8fafc' }}>{run.summary.captures}</strong> captures</span>
          <span>📬 <strong style={{ color: '#f8fafc' }}>{run.summary.accepted}</strong> accepted</span>
          <span>❌ <strong style={{ color: '#f8fafc' }}>{run.summary.failures}</strong> failures</span>
          {run.summary.current_activity && (
            <span style={{ color: '#fbbf24' }}>
              ▶ {run.summary.current_activity.type}
            </span>
          )}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8 }}>
        <button
          onClick={() => onView(run)}
          style={{
            background: 'rgba(125,211,252,0.12)', color: '#7dd3fc',
            border: '1px solid rgba(125,211,252,0.30)', borderRadius: 7,
            padding: '5px 14px', fontSize: 12, cursor: 'pointer', fontWeight: 600,
          }}
        >
          📋 View Log
        </button>
        {isActive && (
          <button
            onClick={() => onStop(run)}
            style={{
              background: 'rgba(239,68,68,0.10)', color: '#f87171',
              border: '1px solid rgba(239,68,68,0.30)', borderRadius: 7,
              padding: '5px 14px', fontSize: 12, cursor: 'pointer', fontWeight: 600,
            }}
          >
            ⏹ Stop
          </button>
        )}
      </div>
    </div>
  );
}

function LogViewer({ run, onClose }: { run: AgentRun; onClose: () => void }) {
  const [markdown, setMarkdown] = useState('');
  const [loading, setLoading] = useState(true);
  const intervalRef = useRef<ReturnType<typeof setInterval>>();

  const load = useCallback(() => {
    fetch(`${BOT_API}/agent-runs/${run.id}/log?limit=100`)
      .then(r => r.text())
      .then(t => { setMarkdown(t); setLoading(false); })
      .catch(() => setLoading(false));
  }, [run.id]);

  useEffect(() => {
    load();
    // Auto-refresh only while agent is active
    if (run.status === 'RUNNING' || run.status === 'DEPLOYING') {
      intervalRef.current = setInterval(load, 5000);
    }
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [load, run.status]);

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)', zIndex: 9999,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div style={{
        background: '#0f172a', border: '1px solid rgba(255,255,255,0.12)',
        borderRadius: 16, width: '90vw', maxWidth: 860, maxHeight: '88vh',
        display: 'flex', flexDirection: 'column' as const, overflow: 'hidden',
      }}>
        {/* Header */}
        <div style={{
          padding: '14px 20px', borderBottom: '1px solid rgba(255,255,255,0.08)',
          display: 'flex', alignItems: 'center', gap: 10,
        }}>
          <AgentTypeBadge type={run.agent_type} />
          <span style={{ fontSize: 13, color: '#94a3b8', flex: 1 }}>
            {run.bot_name || run.agent_id} · run <code style={{ color: '#c4b5fd' }}>{run.run_id}</code>
          </span>
          {(run.status === 'RUNNING' || run.status === 'DEPLOYING') && (
            <span style={{
              width: 8, height: 8, borderRadius: '50%', background: '#22c55e',
              display: 'inline-block', animation: 'pulse 1.5s infinite',
              boxShadow: '0 0 6px #22c55e',
            }} title="Live — auto-refreshing" />
          )}
          <button
            onClick={onClose}
            style={{
              background: 'none', border: 'none', color: '#94a3b8', cursor: 'pointer',
              fontSize: 20, lineHeight: 1, padding: '0 4px',
            }}
          >×</button>
        </div>

        {/* Body */}
        <div style={{
          flex: 1, overflowY: 'auto' as const, padding: '20px 24px',
          fontFamily: 'system-ui, sans-serif', fontSize: 13, lineHeight: 1.65,
          color: '#cbd5e1',
        }}>
          {loading
            ? <p style={{ color: '#6b7280' }}>Loading agent log…</p>
            : (
              <div
                className="ag-markdown"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(markdown) }}
              />
            )
          }
        </div>

        {/* Footer */}
        <div style={{
          padding: '10px 20px', borderTop: '1px solid rgba(255,255,255,0.08)',
          fontSize: 11, color: '#4b5563',
        }}>
          Keys and raw flags are never shown. Log auto-refreshes every 5 s while agent is running.
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Launch form
// ---------------------------------------------------------------------------

type AgentKind = 'attack_defense' | 'challenge_generator';

function LaunchForm({ providers, onLaunched }: {
  providers: Provider[];
  onLaunched: () => void;
}) {
  const [kind, setKind] = useState<AgentKind>('attack_defense');
  const [teamId, setTeamId] = useState(1);
  const [provId, setProvId] = useState('fake');
  const [modelId, setModelId] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const selectedProvider = providers.find(p => p.id === provId);

  async function launch() {
    setLoading(true);
    setStatus(null);
    try {
      const body: Record<string, unknown> = {
        teams: [teamId],
        bot_name: kind === 'attack_defense'
          ? `AI-Attack/Defend (team ${teamId})`
          : 'ChallengeGeneratorAgent',
        planner: 'model',
        agent_type: kind,
        provider: provId,
        model_id: modelId || selectedProvider?.models?.[0] || '',
        actions: kind === 'attack_defense'
          ? ['attack.recon', 'attack.exploit', 'attack.submit_flag',
             'defend.inspect_files', 'defend.snapshot', 'defend.apply_patch',
             'defend.run_checker', 'defend.run_exploit_regression']
          : [],
        target_policy: 'all_opponents',
        target_teams: [],
        loop_interval: 30,
        stop_on_success: false,
        flag_re: 'FLAG\\{[a-f0-9]{32}\\}',
        timeout: 10,
      };
      const r = await fetch(`${BOT_API}/deployments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (d.ok || r.status === 201) {
        setStatus('✅ Agent deployed!');
        onLaunched();
      } else {
        setStatus(`❌ ${d.error || 'Unknown error'}`);
      }
    } catch (e) {
      setStatus(`❌ Network error: ${e}`);
    }
    setLoading(false);
  }

  return (
    <div style={{
      background: 'rgba(255,255,255,0.04)',
      border: '1px solid rgba(255,255,255,0.10)',
      borderRadius: 12, padding: '20px 24px', marginBottom: 20,
    }}>
      <h3 style={{ margin: '0 0 16px', color: '#f8fafc', fontSize: 15 }}>🚀 Launch Agent</h3>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        {/* Kind */}
        <div>
          <label style={labelStyle}>Agent Type</label>
          <select
            id="ag-kind"
            value={kind}
            onChange={e => setKind(e.target.value as AgentKind)}
            style={selectStyle}
          >
            <option value="attack_defense">⚔️ Attack/Defense Agent</option>
            <option value="challenge_generator">🏗️ Challenge Generator</option>
          </select>
        </div>

        {/* Team */}
        <div>
          <label style={labelStyle}>Team</label>
          <input
            id="ag-team"
            type="number" min={1} max={10}
            value={teamId}
            onChange={e => setTeamId(Number(e.target.value))}
            style={inputStyle}
          />
        </div>

        {/* Provider */}
        <div>
          <label style={labelStyle}>Provider</label>
          <select
            id="ag-provider"
            value={provId}
            onChange={e => { setProvId(e.target.value); setModelId(''); }}
            style={selectStyle}
          >
            {providers.map(p => (
              <option key={p.id} value={p.id} disabled={!p.available}>
                {p.available ? '' : '🔒 '}{p.label}
              </option>
            ))}
          </select>
        </div>

        {/* Model */}
        <div>
          <label style={labelStyle}>Model</label>
          <select
            id="ag-model"
            value={modelId}
            onChange={e => setModelId(e.target.value)}
            style={selectStyle}
            disabled={!selectedProvider?.models?.length}
          >
            {(selectedProvider?.models || []).map(m => (
              <option key={m} value={m}>{m}</option>
            ))}
            {!selectedProvider?.models?.length && (
              <option value="">— default —</option>
            )}
          </select>
        </div>
      </div>

      <button
        id="ag-launch-btn"
        onClick={launch}
        disabled={loading}
        style={{
          marginTop: 16,
          background: loading ? 'rgba(99,102,241,0.3)' : 'rgba(99,102,241,0.8)',
          color: '#fff', border: 'none', borderRadius: 8,
          padding: '9px 24px', fontSize: 13, fontWeight: 700, cursor: 'pointer',
          transition: 'all 0.2s',
        }}
      >
        {loading ? 'Launching…' : '🚀 Launch'}
      </button>

      {status && (
        <p style={{ marginTop: 10, fontSize: 13, color: status.startsWith('✅') ? '#22c55e' : '#f87171' }}>
          {status}
        </p>
      )}
    </div>
  );
}

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: 11, color: '#94a3b8',
  marginBottom: 5, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.04em',
};
const selectStyle: React.CSSProperties = {
  width: '100%', background: '#1e293b', color: '#f8fafc',
  border: '1px solid rgba(255,255,255,0.14)', borderRadius: 7,
  padding: '7px 10px', fontSize: 13,
};
const inputStyle: React.CSSProperties = { ...selectStyle };

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export default function AgentsPanel() {
  const providers = useProviders();
  const { runs, refresh } = useAgentRuns(undefined, 5000);
  const [viewing, setViewing] = useState<AgentRun | null>(null);

  async function stopRun(run: AgentRun) {
    await fetch(`${BOT_API}/deployments/${run.id}/stop`, { method: 'POST' });
    refresh();
  }

  const activeRuns = runs.filter(r => r.status === 'RUNNING' || r.status === 'DEPLOYING');
  const finishedRuns = runs.filter(r => r.status !== 'RUNNING' && r.status !== 'DEPLOYING');

  return (
    <div style={{
      fontFamily: "'Inter', system-ui, sans-serif",
      color: '#f8fafc', minHeight: '100%', padding: '24px',
    }}>
      <style>{`
        .ag-markdown h2.ag-md-h1 { color:#f8fafc; font-size:17px; margin:0 0 10px; }
        .ag-markdown h3.ag-md-h2 { color:#7dd3fc; font-size:14px; margin:14px 0 6px; border-bottom:1px solid rgba(125,211,252,0.15); padding-bottom:4px; }
        .ag-markdown .ag-md-hr { border:none; border-top:1px solid rgba(255,255,255,0.08); margin:10px 0; }
        .ag-markdown .ag-md-p { margin:4px 0; }
        .ag-markdown .ag-md-pre { background:#0d1117; border:1px solid rgba(255,255,255,0.1); border-radius:6px; padding:10px 14px; overflow-x:auto; font-size:12px; margin:6px 0; }
        .ag-markdown .ag-md-code { background:rgba(255,255,255,0.08); border-radius:4px; padding:1px 5px; font-family:monospace; }
        .ag-markdown strong { color:#f8fafc; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
      `}</style>

      <div style={{ marginBottom: 28 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, margin: '0 0 4px' }}>
          🤖 AI Agents
        </h1>
        <p style={{ color: '#64748b', fontSize: 13, margin: 0 }}>
          Deploy attack/defense or challenge-generator agents. View live thinking logs.
        </p>
      </div>

      <LaunchForm providers={providers} onLaunched={refresh} />

      {/* Active agents */}
      {activeRuns.length > 0 && (
        <>
          <h2 style={{ fontSize: 14, color: '#22c55e', margin: '0 0 10px', fontWeight: 600 }}>
            🟢 Active ({activeRuns.length})
          </h2>
          {activeRuns.map(r => (
            <AgentCard key={r.id} run={r} onView={setViewing} onStop={stopRun} />
          ))}
        </>
      )}

      {/* Finished */}
      {finishedRuns.length > 0 && (
        <>
          <h2 style={{ fontSize: 14, color: '#6b7280', margin: '16px 0 10px', fontWeight: 600 }}>
            ⏸ History ({finishedRuns.length})
          </h2>
          {finishedRuns.slice(0, 20).map(r => (
            <AgentCard key={r.id} run={r} onView={setViewing} onStop={stopRun} />
          ))}
        </>
      )}

      {runs.length === 0 && (
        <div style={{
          textAlign: 'center' as const, color: '#4b5563', padding: '48px 0', fontSize: 14,
        }}>
          No agents deployed yet. Use the form above to launch one.
        </div>
      )}

      {viewing && <LogViewer run={viewing} onClose={() => setViewing(null)} />}
    </div>
  );
}
