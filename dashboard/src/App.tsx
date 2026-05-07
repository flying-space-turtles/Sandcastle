import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ActionPanel } from './components/ActionPanel'
import { EventLog } from './components/EventLog'
import { Scoreboard } from './components/Scoreboard'
import { PolygonFlow } from './flow/PolygonFlow'
import { api } from './api'
import type { GameserverState } from './types'
import './App.css'

const POLL_INTERVAL = 2000

function App() {
  const [state, setState] = useState<GameserverState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedTeam, setSelectedTeam] = useState<number | null>(null)
  const [actionLog, setActionLog] = useState<string[]>([])
  const [flashTeams, setFlashTeams] = useState<Set<number>>(new Set())

  const previousRound = useRef<number>(-1)

  // Poll the gameserver every POLL_INTERVAL ms.
  useEffect(() => {
    let cancelled = false
    async function tick() {
      try {
        const next = await api.state()
        if (cancelled) return
        setState(next)
        setError(null)
        if (
          previousRound.current !== -1 &&
          next.round !== previousRound.current
        ) {
          // Tick advanced — flash all team edges briefly.
          const ids = new Set(next.teams.map((t) => t.id))
          setFlashTeams(ids)
          setTimeout(() => setFlashTeams(new Set()), 1200)
        }
        previousRound.current = next.round
      } catch (err) {
        if (cancelled) return
        setError((err as Error).message)
      }
    }
    tick()
    const handle = setInterval(tick, POLL_INTERVAL)
    return () => {
      cancelled = true
      clearInterval(handle)
    }
  }, [])

  const onSelectTeam = useCallback((teamId: number) => {
    setSelectedTeam((prev) => (prev === teamId ? null : teamId))
  }, [])

  const selected = useMemo(() => {
    if (!state || selectedTeam == null) return null
    return state.teams.find((t) => t.id === selectedTeam) ?? null
  }, [state, selectedTeam])

  const recordAction = useCallback((kind: string, msg: string) => {
    setActionLog((prev) =>
      [`${new Date().toLocaleTimeString()} ${kind}: ${msg}`, ...prev].slice(0, 20),
    )
  }, [])

  const teams = state?.teams ?? []

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__title">AD-CTF · Defense Polygon</div>
        <div className="app__meta">
          {state ? (
            <>
              <span>Round {state.round}</span>
              <span className="app__sep">·</span>
              <span>{state.config.num_teams} teams</span>
              <span className="app__sep">·</span>
              <span>tick every {state.config.tick_duration}s</span>
              <span className="app__sep">·</span>
              <span className={state.paused ? 'pill pill--warn' : 'pill pill--good'}>
                {state.paused ? 'PAUSED' : 'RUNNING'}
              </span>
            </>
          ) : error ? (
            <span className="pill pill--bad">connecting… {error}</span>
          ) : (
            <span>connecting…</span>
          )}
        </div>
      </header>

      <main className="app__main">
        <div className="app__flow">
          <PolygonFlow
            state={state}
            selectedTeam={selectedTeam}
            onSelectTeam={onSelectTeam}
            flashTeams={flashTeams}
          />
        </div>

        <aside className="app__side">
          <Scoreboard rows={state?.scoreboard ?? []} />
          <ActionPanel
            teams={teams}
            selectedTeam={selected}
            paused={state?.paused ?? false}
            onAction={recordAction}
          />
          <EventLog events={state?.events ?? []} />
          {actionLog.length > 0 && (
            <div className="panel">
              <div className="panel__title">Recent UI actions</div>
              <ul className="action-log">
                {actionLog.map((line, idx) => (
                  <li key={idx}>{line}</li>
                ))}
              </ul>
            </div>
          )}
        </aside>
      </main>
    </div>
  )
}

export default App
