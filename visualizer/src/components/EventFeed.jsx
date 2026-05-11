import { useState } from 'react';

const TYPE_META = {
  sqli: { label: 'SQLi', color: '#ef4444' },
  cmdi: { label: 'CMDi', color: '#f97316' },
  'path-traversal': { label: 'Traversal', color: '#a855f7' },
  ssh: { label: 'SSH', color: '#fbbf24' },
  http: { label: 'HTTP', color: '#38bdf8' },
  tcp: { label: 'TCP', color: '#64748b' },
};

const ALL_TYPES = Object.keys(TYPE_META);

const EventFeed = ({ events, connected }) => {
  const [activeTypes, setActiveTypes] = useState(
    () => new Set(['sqli', 'cmdi', 'path-traversal', 'http']),
  );

  const toggleType = (type) =>
    setActiveTypes((prev) => {
      const next = new Set(prev);
      next.has(type) ? next.delete(type) : next.add(type);
      return next;
    });

  const visible = events
    .filter((e) => activeTypes.has(e.type))
    .sort((a, b) => (b._received ?? b.ts) - (a._received ?? a.ts));

  return (
    <aside className="event-feed">
      <div className="event-feed__header">
        <h2>Live Events</h2>
        <span className={`event-feed__dot ${connected ? 'is-live' : 'is-offline'}`}>
          {connected ? 'Live' : 'Offline'}
        </span>
      </div>

      <div className="event-feed__filters">
        {ALL_TYPES.map((type) => {
          const meta = TYPE_META[type];
          return (
            <button
              key={type}
              type="button"
              className={`event-filter-btn ${activeTypes.has(type) ? 'is-active' : ''}`}
              style={{ '--badge': meta.color }}
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
            : 'Monitor container is not running.'}
        </p>
      ) : (
        <div className="event-feed__list">
          {visible.map((event) => {
            const meta = TYPE_META[event.type] ?? { label: event.type, color: '#64748b' };
            return (
              <div key={event.id} className="event-item">
                <span className="event-item__badge" style={{ '--badge': meta.color }}>
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
