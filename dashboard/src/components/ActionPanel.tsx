import { useState } from 'react'
import { api } from '../api'
import type { TeamView } from '../types'

interface ActionPanelProps {
  teams: TeamView[]
  selectedTeam: TeamView | null
  paused: boolean
  onAction: (kind: string, message: string) => void
}

export function ActionPanel({
  teams,
  selectedTeam,
  paused,
  onAction,
}: ActionPanelProps) {
  const [attackerId, setAttackerId] = useState<number | ''>('')
  const [flagInput, setFlagInput] = useState('')
  const [submitMessage, setSubmitMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const attacker = teams.find((t) => t.id === attackerId)

  async function call<T>(label: string, fn: () => Promise<T>) {
    setBusy(true)
    try {
      const result = await fn()
      onAction('action', `${label}: ok`)
      return result
    } catch (err) {
      const msg = (err as Error).message
      onAction('error', `${label}: ${msg}`)
      throw err
    } finally {
      setBusy(false)
    }
  }

  async function onSubmitFlag() {
    if (!attacker || !flagInput.trim()) {
      setSubmitMessage('pick a team and paste a flag')
      return
    }
    try {
      const r = await call('submit flag', () =>
        api.submitFlag(attacker.submission_token, flagInput.trim()),
      )
      setSubmitMessage(`accepted (round ${r.round})`)
      setFlagInput('')
    } catch (err) {
      setSubmitMessage(`rejected: ${(err as Error).message}`)
    }
  }

  return (
    <div className="panel action-panel">
      <div className="panel__title">Actions</div>

      <div className="action-panel__row">
        <button
          disabled={busy}
          onClick={() => void call('force tick', api.forceTick)}
        >
          Force tick
        </button>
        {paused ? (
          <button
            disabled={busy}
            onClick={() => void call('resume', api.resume)}
          >
            Resume
          </button>
        ) : (
          <button
            disabled={busy}
            onClick={() => void call('pause', api.pause)}
          >
            Pause
          </button>
        )}
      </div>

      <div className="action-panel__divider">Selected team</div>

      {selectedTeam ? (
        <div className="action-panel__team">
          <div>
            <strong>{selectedTeam.name}</strong> — {selectedTeam.ip_address}
          </div>
          <div className="action-panel__hint">
            container <code>{selectedTeam.container.vuln}</code> · state{' '}
            <code>{selectedTeam.container.vuln_state}</code>
          </div>
          <div className="action-panel__row">
            <button
              disabled={busy}
              onClick={() =>
                void call('take down', () => api.takeDown(selectedTeam.id))
              }
            >
              Take down
            </button>
            <button
              disabled={busy}
              onClick={() =>
                void call('bring up', () => api.bringUp(selectedTeam.id))
              }
            >
              Bring up
            </button>
            <button
              disabled={busy}
              onClick={() =>
                void call('restart', () => api.restart(selectedTeam.id))
              }
            >
              Restart
            </button>
          </div>
        </div>
      ) : (
        <div className="action-panel__hint">Click a node to select a team.</div>
      )}

      <div className="action-panel__divider">Submit a flag</div>
      <div className="action-panel__field">
        <label>Submitter</label>
        <select
          value={attackerId}
          onChange={(e) =>
            setAttackerId(e.target.value === '' ? '' : Number(e.target.value))
          }
        >
          <option value="">— select team —</option>
          {teams.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </div>
      <div className="action-panel__field">
        <label>Flag</label>
        <input
          spellCheck={false}
          placeholder="FLAG{...}"
          value={flagInput}
          onChange={(e) => setFlagInput(e.target.value)}
        />
      </div>
      <div className="action-panel__row">
        <button disabled={busy || !attacker || !flagInput} onClick={onSubmitFlag}>
          Submit flag
        </button>
      </div>
      {submitMessage && (
        <div className="action-panel__notice">{submitMessage}</div>
      )}
    </div>
  )
}
