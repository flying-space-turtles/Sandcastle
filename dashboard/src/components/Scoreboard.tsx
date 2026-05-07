import type { ScoreboardRow } from '../types'

interface ScoreboardProps {
  rows: ScoreboardRow[]
}

export function Scoreboard({ rows }: ScoreboardProps) {
  return (
    <div className="panel">
      <div className="panel__title">Scoreboard</div>
      <table className="scoreboard">
        <thead>
          <tr>
            <th>#</th>
            <th>Team</th>
            <th>Atk</th>
            <th>Def</th>
            <th>SLA</th>
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.team_id}>
              <td>{i + 1}</td>
              <td>{r.name}</td>
              <td>{r.attack.toFixed(1)}</td>
              <td>{r.defense.toFixed(1)}</td>
              <td>{r.sla.toFixed(1)}</td>
              <td className="scoreboard__total">{r.total.toFixed(1)}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} className="scoreboard__empty">
                No scores yet — wait for the first tick.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
