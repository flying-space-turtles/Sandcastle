import { Flag, Radio, RefreshCw, Wifi, WifiOff } from 'lucide-react';
import { memo } from 'react';

const TYPE_META = {
  flag:      { icon: Flag,      label: 'FLAG',     cls: 'event--flag' },
  probe:     { icon: Radio,     label: 'PROBE',    cls: 'event--probe' },
  watchdog:  { icon: RefreshCw, label: 'WATCHDOG', cls: 'event--watchdog' },
  fail:      { icon: WifiOff,   label: 'FAIL',     cls: 'event--fail' },
  sleep:     { icon: Wifi,      label: 'SLEEP',    cls: 'event--sleep' },
  ping_up:   { icon: Wifi,      label: 'PING ↑',   cls: 'event--ping-up' },
  ping_down: { icon: WifiOff,   label: 'PING ↓',   cls: 'event--ping-down' },
};

const fmt = (ts) => {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};

const EventRow = ({ ev }) => {
  const meta = TYPE_META[ev.type] || { label: ev.type.toUpperCase(), cls: '' };
  const Icon = meta.icon;
  return (
    <div className={`event-row ${meta.cls}`}>
      <span className="event-row__time">{fmt(ev.ts)}</span>
      <span className="event-row__tag">
        {Icon && <Icon size={11} strokeWidth={2.4} />}
        {meta.label}
      </span>
      <span className="event-row__attacker">{ev.attacker}</span>
      <span className="event-row__msg">{ev.msg}</span>
    </div>
  );
};

const EventLog = ({ events, connected }) => (
  <aside className="event-log">
    <div className="event-log__header">
      <span>Attack Log</span>
      <span className={`event-log__status ${connected ? 'is-live' : 'is-offline'}`}>
        {connected ? 'LIVE' : 'OFFLINE'}
      </span>
    </div>
    {events.length === 0 ? (
      <div className="event-log__empty">
        {connected ? 'Waiting for bot activity…' : 'Start server.py to stream events.'}
      </div>
    ) : (
      <div className="event-log__feed">
        {[...events].reverse().map((ev, i) => (
          <EventRow key={`${ev.ts}-${i}`} ev={ev} />
        ))}
      </div>
    )}
  </aside>
);

export default memo(EventLog);
