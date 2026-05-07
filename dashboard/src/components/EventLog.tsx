import type { EventRow } from '../types'

interface EventLogProps {
  events: EventRow[]
}

const KIND_TONE: Record<string, string> = {
  'flag.planted': 'good',
  'flag.captured': 'attack',
  'flag.plant_failed': 'bad',
  'sla.up': 'good',
  'sla.down': 'bad',
  'sla.corrupt': 'warn',
  'sla.mumble': 'warn',
  'tick.start': 'info',
  'tick.end': 'info',
  'tick.skip': 'warn',
  'tick.paused': 'warn',
  'tick.resumed': 'info',
  'container.stopped': 'warn',
  'container.started': 'info',
  'container.restarted': 'info',
  'server.start': 'info',
  'server.stop': 'warn',
}

function tone(kind: string): string {
  return KIND_TONE[kind] ?? 'info'
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString(undefined, { hour12: false })
}

export function EventLog({ events }: EventLogProps) {
  return (
    <div className="panel">
      <div className="panel__title">Event log</div>
      <div className="event-log">
        {events.map((e) => (
          <div key={e.id} className={`event-log__row event-log__row--${tone(e.kind)}`}>
            <span className="event-log__time">{formatTime(e.created_at)}</span>
            <span className="event-log__kind">{e.kind}</span>
            <span className="event-log__msg">{e.message}</span>
          </div>
        ))}
        {events.length === 0 && (
          <div className="event-log__empty">No events yet.</div>
        )}
      </div>
    </div>
  )
}
