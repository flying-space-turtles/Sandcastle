import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import {
  Activity,
  ChevronDown,
  CircleStop,
  Clock3,
  Gauge,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Settings2,
  ShieldCheck,
  SkipForward,
  Trophy,
  X,
} from 'lucide-react';
import { botApiUrl, gameserverApiUrl } from '../data/arenaConfig';

type MatchStatus = 'CREATED' | 'RUNNING' | 'PAUSED' | 'FINISHED' | 'FAILED';
type CheckerStatus = 'UP' | 'DOWN' | 'MUMBLE' | 'CORRUPT' | 'PENDING';

interface DashboardSnapshot {
  match: { match_id: number; status: MatchStatus; created_at: string; updated_at: string };
  round: {
    round_number: number;
    status: 'RUNNING' | 'COMPLETED' | 'FAILED';
    started_at: string;
    deadline_at: string;
    completed_at: string | null;
    duration_seconds: number;
    error: string | null;
  } | null;
  policy: { version: string; attack_points: number; defense_points: number; sla_points: number };
  standings: Array<{
    rank: number;
    team_id: number;
    team_name: string;
    attack: number;
    defense: number;
    sla: number;
    total: number;
  }>;
  services: Array<{
    team_id: number;
    team_name: string;
    service_id: number;
    service_name: string;
    port: number;
    round_number: number | null;
    status: CheckerStatus;
    operations: Record<string, {
      status: CheckerStatus;
      message: string;
      duration_ms: number;
      created_at: string;
    }>;
    last_checked_at: string | null;
  }>;
}

interface MatchPlanSnapshot {
  assignments: Array<{ team_id: number; assignment_kind: string }>;
  deployed_challenge: { vulnerability?: string; challenge_id?: string } | null;
  latest_published_challenge: { id?: string; vulnerability?: string; challenge_id?: string; deployed_at?: string | null } | null;
}

const POLL_INTERVAL_MS = 2500;
const STALE_AFTER_MS = 8000;
const formatScore = (value: number) => Number.isInteger(value) ? value.toString() : value.toFixed(2);
const formatCountdown = (deadline: string, now: number) => {
  const seconds = Math.ceil(Math.max(0, Date.parse(deadline) - now) / 1000);
  return `${Math.floor(seconds / 60)}:${(seconds % 60).toString().padStart(2, '0')}`;
};

const Scoreboard = () => {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [operatorToken, setOperatorToken] = useState(
    () => sessionStorage.getItem('sandcastle.operatorToken') || '',
  );
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [controlsOpen, setControlsOpen] = useState(false);
  const [startQueuedAgents, setStartQueuedAgents] = useState(true);
  const [deployLatestChallenge, setDeployLatestChallenge] = useState(true);
  const [matchPlan, setMatchPlan] = useState<MatchPlanSnapshot | null>(null);
  const [now, setNow] = useState(Date.now());

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${gameserverApiUrl}/api/dashboard`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`gameserver returned HTTP ${response.status}`);
      setSnapshot(await response.json() as DashboardSnapshot);
      setLastUpdated(Date.now());
      setFetchError(null);
    } catch (error) {
      setFetchError(error instanceof Error ? error.message : 'could not reach gameserver');
    }
  }, []);

  useEffect(() => {
    void refresh();
    const poll = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(clock);
    };
  }, [refresh]);

  useEffect(() => {
    if (operatorToken) sessionStorage.setItem('sandcastle.operatorToken', operatorToken);
    else sessionStorage.removeItem('sandcastle.operatorToken');
  }, [operatorToken]);

  useEffect(() => {
    if (!controlsOpen) return;
    fetch(`${botApiUrl}/match-plan`, { cache: 'no-store' })
      .then((response) => response.ok ? response.json() : null)
      .then((body) => setMatchPlan(body as MatchPlanSnapshot | null))
      .catch(() => setMatchPlan(null));
  }, [controlsOpen]);

  const runAction = async (action: 'start' | 'pause' | 'resume' | 'step' | 'finish' | 'restart') => {
    if (!operatorToken) {
      setActionError('Enter the operator token before using match controls.');
      return;
    }
    if (
      action === 'restart' &&
      !window.confirm('Restart this match? All rounds, flags, submissions, checker results, and scores will be cleared.')
    ) {
      return;
    }
    setPendingAction(action);
    setActionError(null);
    try {
      if (action === 'start' && (deployLatestChallenge || startQueuedAgents)) {
        const planResponse = await fetch(`${botApiUrl}/match-plan/prepare`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            deploy_latest_challenge: deployLatestChallenge,
            start_agents: startQueuedAgents,
          }),
        });
        const planBody = await planResponse.json().catch(() => ({})) as { error?: string; output?: string };
        if (!planResponse.ok) {
          const detail = planBody.error || planBody.output || `match prepare failed with HTTP ${planResponse.status}`;
          throw new Error(detail.length > 1200 ? `${detail.slice(0, 1200)}...` : detail);
        }
      }
      const path = action === 'step' ? '/api/rounds/step' : `/api/match/${action}`;
      const response = await fetch(`${gameserverApiUrl}${path}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${operatorToken}`, 'Content-Type': 'application/json' },
        body: '{}',
      });
      const body = await response.json().catch(() => ({})) as { error?: string; code?: string };
      if (!response.ok) throw new Error(body.error || body.code || `HTTP ${response.status}`);
      await refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'operator action failed');
    } finally {
      setPendingAction(null);
    }
  };

  const stale = lastUpdated === null || now - lastUpdated > STALE_AFTER_MS;
  const matchStatus = snapshot?.match.status;
  const maxTotal = Math.max(1, ...(snapshot?.standings.map((team) => team.total) || [1]));
  const healthyServices = snapshot?.services.filter((service) => service.status === 'UP').length || 0;
  const roundLabel = useMemo(() => {
    if (!snapshot?.round) return 'Waiting for first round';
    if (matchStatus === 'PAUSED') return 'Scheduler paused';
    if (matchStatus === 'RUNNING') return `${formatCountdown(snapshot.round.deadline_at, now)} remaining`;
    return snapshot.round.status;
  }, [matchStatus, now, snapshot?.round]);

  return (
    <main className="match-page">
      <section className="match-hero">
        <div className="match-hero__copy">
          <span className="page-kicker">Live competition control</span>
          <h1>Match operations</h1>
          <p>Follow the race, spot service failures, and control the round scheduler from one view.</p>
        </div>
        <div className="match-hero__actions">
          <div className={`connection-pill ${fetchError || stale ? 'is-stale' : 'is-live'}`} role="status">
            <span />
            {fetchError ? 'API unavailable' : stale ? 'Data stale' : 'Live'}
          </div>
          <button className="icon-button" type="button" onClick={() => void refresh()} title="Refresh">
            <RefreshCw size={17} />
          </button>
          <button className="primary-button" type="button" onClick={() => setControlsOpen(true)}>
            <Settings2 size={16} /> Match controls
          </button>
        </div>
      </section>

      {(fetchError || actionError) && (
        <div className="inline-alert" role="alert">{actionError || fetchError}</div>
      )}

      <section className="match-pulse">
        <div className={`match-state state-${(matchStatus || 'unknown').toLowerCase()}`}>
          <div className="match-state__icon"><Activity size={21} /></div>
          <div>
            <span>Match state</span>
            <strong>{matchStatus || 'Connecting'}</strong>
          </div>
        </div>
        <div className="pulse-metric">
          <span>Round</span>
          <strong>{snapshot?.round ? `#${snapshot.round.round_number}` : '—'}</strong>
          <small>{roundLabel}</small>
        </div>
        <div className="pulse-metric">
          <span>Service health</span>
          <strong>{snapshot ? `${healthyServices}/${snapshot.services.length}` : '—'}</strong>
          <small>checkers reporting UP</small>
        </div>
        <div className="pulse-metric">
          <span>Scoring</span>
          <strong>{snapshot?.policy.version || '—'}</strong>
          <small>
            {snapshot ? `Attack ${snapshot.policy.attack_points} · Defense ${snapshot.policy.defense_points} · SLA ${snapshot.policy.sla_points}` : 'Waiting for policy'}
          </small>
        </div>
      </section>

      <section className="match-layout">
        <div className="leaderboard">
          <div className="section-heading">
            <div><span className="section-icon"><Trophy size={17} /></span><div><h2>Leaderboard</h2><p>Score composition across the arena</p></div></div>
            <small>{lastUpdated ? `Updated ${Math.floor((now - lastUpdated) / 1000)}s ago` : 'Connecting'}</small>
          </div>
          <div className="leader-list">
            {snapshot?.standings.map((team) => (
              <article className={`leader-card rank-${team.rank}`} key={team.team_id}>
                <div className="leader-rank">{team.rank}</div>
                <div className="leader-identity">
                  <strong>{team.team_name}</strong>
                  <span>Team {team.team_id}</span>
                </div>
                <div className="leader-composition">
                  <div className="score-track" aria-label={`${team.team_name} score composition`}>
                    <i className="is-attack" style={{ '--score-width': `${(team.attack / maxTotal) * 100}%` } as CSSProperties} />
                    <i className="is-defense" style={{ '--score-width': `${(team.defense / maxTotal) * 100}%` } as CSSProperties} />
                    <i className="is-sla" style={{ '--score-width': `${(team.sla / maxTotal) * 100}%` } as CSSProperties} />
                  </div>
                  <div className="score-legend">
                    <span><i className="is-attack" />A {formatScore(team.attack)}</span>
                    <span><i className="is-defense" />D {formatScore(team.defense)}</span>
                    <span><i className="is-sla" />SLA {formatScore(team.sla)}</span>
                  </div>
                </div>
                <div className="leader-total"><strong>{formatScore(team.total)}</strong><span>points</span></div>
              </article>
            ))}
            {snapshot && snapshot.standings.length === 0 && <div className="empty-state">No teams are registered.</div>}
            {!snapshot && <div className="loading-block">Loading standings…</div>}
          </div>
          {snapshot && snapshot.standings.length > 0 && (
            <details className="score-details">
              <summary><ChevronDown size={15} /> Detailed scores</summary>
              <div>
                {snapshot.standings.map((team) => (
                  <span key={team.team_id}><b>#{team.rank} {team.team_name}</b><em>{formatScore(team.attack)} attack</em><em>{formatScore(team.defense)} defense</em><em>{formatScore(team.sla)} SLA</em><strong>{formatScore(team.total)}</strong></span>
                ))}
              </div>
            </details>
          )}
        </div>

        <aside className="service-health">
          <div className="section-heading">
            <div><span className="section-icon"><ShieldCheck size={17} /></span><div><h2>Service health</h2><p>Latest checker outcomes</p></div></div>
          </div>
          <div className="health-list">
            {snapshot?.services.map((service) => (
              <article className="health-card" key={`${service.team_id}-${service.service_id}`}>
                <div className="health-card__top">
                  <div><strong>{service.team_name}</strong><span>{service.service_name}:{service.port}</span></div>
                  <span className={`health-status status-${service.status.toLowerCase()}`}>{service.status}</span>
                </div>
                <div className="operation-flow">
                  {['PUT', 'CHECK', 'GET'].map((operation) => {
                    const result = service.operations[operation];
                    return (
                      <div key={operation} className={`operation-step status-${(result?.status || 'PENDING').toLowerCase()}`} title={result?.message}>
                        <span>{operation}</span><strong>{result?.status || 'PENDING'}</strong>
                      </div>
                    );
                  })}
                </div>
                <small><Clock3 size={12} />{service.last_checked_at ? new Date(service.last_checked_at).toLocaleTimeString() : 'No result yet'}</small>
              </article>
            ))}
            {snapshot && snapshot.services.length === 0 && <div className="empty-state">No services registered.</div>}
          </div>
        </aside>
      </section>

      {controlsOpen && (
        <div className="drawer-backdrop" onMouseDown={() => setControlsOpen(false)}>
          <aside className="ops-drawer" onMouseDown={(event) => event.stopPropagation()} aria-label="Match controls">
            <div className="drawer-heading">
              <div><span className="section-icon"><Gauge size={18} /></span><div><h2>Match controls</h2><p>Authenticated scheduler operations</p></div></div>
              <button className="icon-button" type="button" onClick={() => setControlsOpen(false)}><X size={18} /></button>
            </div>
            <label className="drawer-field">
              <span>Operator token</span>
              <input type="password" value={operatorToken} autoComplete="off" placeholder="Required for controls" onChange={(event) => setOperatorToken(event.target.value)} />
            </label>
            <div className="control-summary">
              <span>Current state<strong>{matchStatus || 'Unknown'}</strong></span>
              <span>Round<strong>{snapshot?.round ? `#${snapshot.round.round_number}` : 'Not started'}</strong></span>
            </div>
            <div className="control-summary">
              <span>Challenge<strong>{matchPlan?.deployed_challenge?.vulnerability?.replaceAll('_', ' ') || 'Default service'}</strong></span>
              <span>Queued agents<strong>{matchPlan?.assignments.length ?? 0}</strong></span>
            </div>
            <div className="control-summary">
              <span>Latest published<strong>{matchPlan?.latest_published_challenge?.vulnerability?.replaceAll('_', ' ') || 'None'}</strong></span>
              <span>Arena deploy<strong>{matchPlan?.latest_published_challenge?.deployed_at ? 'Current' : matchPlan?.latest_published_challenge ? 'Pending' : 'Default'}</strong></span>
            </div>
            <label className="check-field" style={{ margin: '12px 24px 0' }}>
              <input type="checkbox" checked={deployLatestChallenge} onChange={(event) => setDeployLatestChallenge(event.target.checked)} />
              Deploy newest published challenge and restart arena before Start match
            </label>
            <label className="check-field" style={{ margin: '12px 24px 0' }}>
              <input type="checkbox" checked={startQueuedAgents} onChange={(event) => setStartQueuedAgents(event.target.checked)} />
              Start queued bots and agents before Start match creates round 1
            </label>
            <div className="control-actions" role="group" aria-label="Scheduler actions">
              <button disabled={matchStatus !== 'CREATED' || pendingAction !== null} onClick={() => void runAction('start')}><Play size={16} />Start match</button>
              <button disabled={matchStatus !== 'RUNNING' || pendingAction !== null} onClick={() => void runAction('pause')}><Pause size={16} />Pause</button>
              <button disabled={matchStatus !== 'PAUSED' || pendingAction !== null} onClick={() => void runAction('resume')}><Play size={16} />Resume</button>
              <button disabled={matchStatus !== 'PAUSED' || pendingAction !== null} onClick={() => void runAction('step')}><SkipForward size={16} />Run one round</button>
              <button className="is-danger" disabled={!['RUNNING', 'PAUSED'].includes(matchStatus || '') || pendingAction !== null} onClick={() => void runAction('finish')}><CircleStop size={16} />Finish match</button>
              <button className="is-danger" disabled={!['FINISHED', 'FAILED'].includes(matchStatus || '') || pendingAction !== null} onClick={() => void runAction('restart')}><RotateCcw size={16} />Restart match</button>
            </div>
            <div className="drawer-status" role="status">
              {pendingAction ? `Running ${pendingAction}…` : actionError || 'Controls are ready.'}
            </div>
          </aside>
        </div>
      )}
    </main>
  );
};

export default Scoreboard;
