import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CircleStop,
  Clock3,
  Pause,
  Play,
  RefreshCw,
  ShieldCheck,
  SkipForward,
  Trophy,
} from 'lucide-react';
import { gameserverApiUrl } from '../data/arenaConfig';

type MatchStatus = 'CREATED' | 'RUNNING' | 'PAUSED' | 'FINISHED' | 'FAILED';
type CheckerStatus = 'UP' | 'DOWN' | 'MUMBLE' | 'CORRUPT' | 'PENDING';

interface DashboardSnapshot {
  match: {
    match_id: number;
    status: MatchStatus;
    created_at: string;
    updated_at: string;
  };
  round: {
    round_number: number;
    status: 'RUNNING' | 'COMPLETED' | 'FAILED';
    started_at: string;
    deadline_at: string;
    completed_at: string | null;
    duration_seconds: number;
    error: string | null;
  } | null;
  policy: {
    version: string;
    attack_points: number;
    defense_points: number;
    sla_points: number;
  };
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
    operations: Record<
      string,
      {
        status: CheckerStatus;
        message: string;
        duration_ms: number;
        created_at: string;
      }
    >;
    last_checked_at: string | null;
  }>;
}

const POLL_INTERVAL_MS = 2500;
const STALE_AFTER_MS = 8000;

const formatScore = (value: number) =>
  Number.isInteger(value) ? value.toString() : value.toFixed(2);

const formatCountdown = (deadline: string, now: number) => {
  const remaining = Math.max(0, Date.parse(deadline) - now);
  const totalSeconds = Math.ceil(remaining / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
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
  const [now, setNow] = useState(Date.now());

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${gameserverApiUrl}/api/dashboard`, {
        cache: 'no-store',
      });
      if (!response.ok) {
        throw new Error(`gameserver returned HTTP ${response.status}`);
      }
      const body = (await response.json()) as DashboardSnapshot;
      setSnapshot(body);
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
    if (operatorToken) {
      sessionStorage.setItem('sandcastle.operatorToken', operatorToken);
    } else {
      sessionStorage.removeItem('sandcastle.operatorToken');
    }
  }, [operatorToken]);

  const runAction = async (action: 'start' | 'pause' | 'resume' | 'step' | 'finish') => {
    if (!operatorToken) {
      setActionError('Enter the operator token before using match controls.');
      return;
    }
    setPendingAction(action);
    setActionError(null);
    const path = action === 'step' ? '/api/rounds/step' : `/api/match/${action}`;
    try {
      const response = await fetch(`${gameserverApiUrl}${path}`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${operatorToken}`,
          'Content-Type': 'application/json',
        },
        body: '{}',
      });
      const body = (await response.json().catch(() => ({}))) as {
        error?: string;
        code?: string;
      };
      if (!response.ok) {
        throw new Error(body.error || body.code || `HTTP ${response.status}`);
      }
      await refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'operator action failed');
    } finally {
      setPendingAction(null);
    }
  };

  const stale = lastUpdated === null || now - lastUpdated > STALE_AFTER_MS;
  const matchStatus = snapshot?.match.status;
  const roundLabel = useMemo(() => {
    if (!snapshot?.round) {
      return 'No round yet';
    }
    if (matchStatus === 'PAUSED') {
      return 'Scheduler paused';
    }
    return snapshot.round.status === 'RUNNING' ? 'Round deadline' : 'Next round';
  }, [matchStatus, snapshot?.round]);

  return (
    <main className="scoreboard">
      <section className="scoreboard__hero">
        <div>
          <div className="scoreboard__eyebrow">Authoritative gameserver state</div>
          <h1>Match Scoreboard</h1>
          <p>Scores and checker outcomes refresh automatically without reloading the page.</p>
        </div>
        <div className="scoreboard__connection">
          <span className={`live-indicator ${fetchError || stale ? 'is-stale' : 'is-live'}`}>
            {fetchError ? 'API unavailable' : stale ? 'Data stale' : 'Live'}
          </span>
          <button type="button" onClick={() => void refresh()} title="Refresh scoreboard">
            <RefreshCw size={15} />
            Refresh
          </button>
          <small>
            {lastUpdated ? `Updated ${Math.max(0, Math.floor((now - lastUpdated) / 1000))}s ago` : 'Never updated'}
          </small>
        </div>
      </section>

      {(fetchError || actionError) && (
        <div className="scoreboard__error" role="alert">
          {actionError || fetchError}
        </div>
      )}

      <section className="match-overview">
        <div className="overview-card">
          <span>Match state</span>
          <strong className={`status-text status-${(matchStatus || 'UNKNOWN').toLowerCase()}`}>
            {matchStatus || 'UNKNOWN'}
          </strong>
        </div>
        <div className="overview-card">
          <span>Current round</span>
          <strong>{snapshot?.round ? `#${snapshot.round.round_number}` : '-'}</strong>
          <small>{snapshot?.round?.status || 'Waiting to start'}</small>
        </div>
        <div className="overview-card">
          <span>{roundLabel}</span>
          <strong>
            {snapshot?.round && matchStatus === 'RUNNING'
              ? formatCountdown(snapshot.round.deadline_at, now)
              : '--:--'}
          </strong>
          <small>{snapshot?.round ? `${snapshot.round.duration_seconds}s rounds` : 'Start the match to create one'}</small>
        </div>
        <div className="overview-card">
          <span>Scoring policy</span>
          <strong>{snapshot?.policy.version || '-'}</strong>
          <small>
            {snapshot
              ? `A ${formatScore(snapshot.policy.attack_points)} · D ${formatScore(snapshot.policy.defense_points)} · SLA ${formatScore(snapshot.policy.sla_points)}`
              : 'Waiting for gameserver'}
          </small>
        </div>
      </section>

      <section className="operator-console">
        <div className="operator-console__credential">
          <label htmlFor="operator-token">Operator token</label>
          <input
            id="operator-token"
            type="password"
            value={operatorToken}
            autoComplete="off"
            placeholder="Required for match controls"
            onChange={(event) => setOperatorToken(event.target.value)}
          />
        </div>
        <div className="operator-console__actions">
          <button
            type="button"
            disabled={matchStatus !== 'CREATED' || pendingAction !== null}
            onClick={() => void runAction('start')}
          >
            <Play size={15} /> Start
          </button>
          <button
            type="button"
            disabled={matchStatus !== 'RUNNING' || pendingAction !== null}
            onClick={() => void runAction('pause')}
          >
            <Pause size={15} /> Pause
          </button>
          <button
            type="button"
            disabled={matchStatus !== 'PAUSED' || pendingAction !== null}
            onClick={() => void runAction('resume')}
          >
            <Play size={15} /> Resume
          </button>
          <button
            type="button"
            disabled={matchStatus !== 'PAUSED' || pendingAction !== null}
            onClick={() => void runAction('step')}
          >
            <SkipForward size={15} /> Step
          </button>
          <button
            type="button"
            className="is-danger"
            disabled={!['RUNNING', 'PAUSED'].includes(matchStatus || '') || pendingAction !== null}
            onClick={() => void runAction('finish')}
          >
            <CircleStop size={15} /> Finish
          </button>
        </div>
      </section>

      <div className="scoreboard__grid">
        <section className="score-panel">
          <div className="score-panel__heading">
            <Trophy size={18} />
            <div>
              <h2>Standings</h2>
              <p>Attack, defense, and SLA components are shown independently.</p>
            </div>
          </div>
          <div className="standings-table">
            <div className="standings-row standings-row--header">
              <span>Rank</span>
              <span>Team</span>
              <span>Attack</span>
              <span>Defense</span>
              <span>SLA</span>
              <span>Total</span>
            </div>
            {snapshot?.standings.map((team) => (
              <div className="standings-row" key={team.team_id}>
                <strong>#{team.rank}</strong>
                <span>{team.team_name}</span>
                <span>{formatScore(team.attack)}</span>
                <span>{formatScore(team.defense)}</span>
                <span>{formatScore(team.sla)}</span>
                <strong>{formatScore(team.total)}</strong>
              </div>
            ))}
            {snapshot && snapshot.standings.length === 0 && (
              <div className="score-panel__empty">No teams are registered.</div>
            )}
          </div>
        </section>

        <section className="score-panel">
          <div className="score-panel__heading">
            <ShieldCheck size={18} />
            <div>
              <h2>Service Checkers</h2>
              <p>Latest PUT, CHECK, and GET results for the displayed round.</p>
            </div>
          </div>
          <div className="service-list">
            {snapshot?.services.map((service) => (
              <article className="service-card" key={`${service.team_id}-${service.service_id}`}>
                <div className="service-card__header">
                  <div>
                    <strong>{service.team_name}</strong>
                    <span>{service.service_name}:{service.port}</span>
                  </div>
                  <span className={`checker-badge status-${service.status.toLowerCase()}`}>
                    {service.status}
                  </span>
                </div>
                <div className="service-card__operations">
                  {['PUT', 'CHECK', 'GET'].map((operation) => {
                    const result = service.operations[operation];
                    return (
                      <span
                        key={operation}
                        className={`checker-operation status-${(result?.status || 'PENDING').toLowerCase()}`}
                        title={result?.message || `${operation} has not completed`}
                      >
                        {operation} {result?.status || 'PENDING'}
                      </span>
                    );
                  })}
                </div>
                <small>
                  <Clock3 size={12} />
                  {service.last_checked_at
                    ? new Date(service.last_checked_at).toLocaleTimeString()
                    : 'No checker result yet'}
                </small>
              </article>
            ))}
            {snapshot && snapshot.services.length === 0 && (
              <div className="score-panel__empty">No services are registered.</div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
};

export default Scoreboard;
