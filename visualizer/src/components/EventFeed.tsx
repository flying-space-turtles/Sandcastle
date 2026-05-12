import { useState, type CSSProperties } from 'react';
import type { EventType, LiveEvent } from '../types';

const TYPE_META: Record<EventType, { label: string; color: string }> = {
  ssh: { label: 'SSH', color: '#fbbf24' },
  telnet: { label: 'Telnet', color: '#f97316' },
  ftp: { label: 'FTP', color: '#a78bfa' },
  smtp: { label: 'SMTP', color: '#e879f9' },
  mysql: { label: 'MySQL', color: '#f43f5e' },
  postgres: { label: 'Postgres', color: '#3b82f6' },
  redis: { label: 'Redis', color: '#ef4444' },
  dns: { label: 'DNS', color: '#10b981' },
  http: { label: 'HTTP', color: '#38bdf8' },
  udp: { label: 'UDP', color: '#84cc16' },
  icmp: { label: 'ICMP', color: '#22c55e' },
  tcp: { label: 'TCP', color: '#64748b' },
};

const ALL_TYPES = Object.keys(TYPE_META) as EventType[];

type EventFeedProps = {
  events: LiveEvent[];
  connected: boolean;
};

const EventFeed = ({ events, connected }: EventFeedProps) => {
  const [activeTypes, setActiveTypes] = useState<Set<EventType>>(
    () => new Set<EventType>(['ssh', 'telnet', 'ftp', 'smtp', 'mysql', 'postgres', 'redis', 'dns', 'http', 'udp', 'icmp', 'tcp']),
  );

  const toggleType = (type: EventType) =>
    setActiveTypes((prev) => {
      const next = new Set(prev);
      next.has(type) ? next.delete(type) : next.add(type);
      return next;
    });

  const visible = events
    .filter((event) => activeTypes.has(event.type as EventType))
    .sort((a, b) => (b.ts - a.ts) || ((b._received ?? 0) - (a._received ?? 0)));

  return (
    <aside className="event-feed">
      <div className="event-feed__header">
        <h2>Activity Log</h2>
        <span className={`event-feed__dot ${connected ? 'is-live' : 'is-offline'}`}>
          {connected ? 'Live' : 'Offline'}
        </span>
      </div>

      <div className="event-feed__filters">
        <button
          type="button"
          className="event-filter-ctrl"
          onClick={() => setActiveTypes(new Set(ALL_TYPES))}
        >All</button>
        <button
          type="button"
          className="event-filter-ctrl"
          onClick={() => setActiveTypes(new Set())}
        >None</button>
        <span className="event-filter-divider" />
        {ALL_TYPES.map((type) => {
          const meta = TYPE_META[type];
          return (
            <button
              key={type}
              type="button"
              className={`event-filter-btn ${activeTypes.has(type) ? 'is-active' : ''}`}
              style={{ '--badge': meta.color } as CSSProperties}
              onClick={() => toggleType(type)}
            >
              {meta.label}
            </button>
          );
        })}
      </div>

      {visible.length === 0 ? (
        <p className="event-feed__empty">
          {connected
            ? activeTypes.size === 0
              ? 'All types filtered out.'
              : 'Waiting for matching events\u2026'
            : 'Firewall container is not running.'}
        </p>
      ) : (
        <div className="event-feed__list">
          {visible.map((event) => {
            const meta = TYPE_META[event.type as EventType] ?? { label: event.type, color: '#64748b' };
            return (
              <div key={event.id} className="event-item">
                <span className="event-item__badge" style={{ '--badge': meta.color } as CSSProperties}>
                  {meta.label}
                </span>
                <div className="event-item__route">
                  <span className="event-item__node">{event.src}</span>
                  <span className="event-item__arrow">&rarr;</span>
                  <span className="event-item__node">{event.dst}</span>
                  <span className="event-item__port">:{event.port}</span>
                </div>
                <span className="event-item__time">
                  {new Date(event.ts * 1000).toLocaleTimeString()}
                </span>
                {event.maskedSrcIp && (
                  <span className="event-item__mask">masked as {event.maskedSrcIp}</span>
                )}
                {event.detail && (
                  <code className="event-item__detail">{event.detail}</code>
                )}
              </div>
            );
          })}
        </div>
      )}
    </aside>
  );
};

export default EventFeed;
