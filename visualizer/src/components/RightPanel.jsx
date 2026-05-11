import { ChevronDown, ChevronRight, Flag, Radio, RefreshCw, Wifi, WifiOff } from 'lucide-react';
import { useState } from 'react';

// ── shared helpers ─────────────────────────────────────────────────────────

const TYPE_META = {
  flag:      { icon: Flag,      label: 'FLAG',     cls: 'event--flag' },
  probe:     { icon: Radio,     label: 'PROBE',    cls: 'event--probe' },
  watchdog:  { icon: RefreshCw, label: 'WATCHDOG', cls: 'event--watchdog' },
  fail:      { icon: WifiOff,   label: 'FAIL',     cls: 'event--fail' },
  sleep:     { icon: Wifi,      label: 'SLEEP',    cls: 'event--sleep' },
  ping_up:   { icon: Wifi,      label: 'PING ↑',   cls: 'event--ping-up' },
  ping_down: { icon: WifiOff,   label: 'PING ↓',   cls: 'event--ping-down' },
};

const fmt = (ts) =>
  new Date(ts * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });

// ── EventLog ───────────────────────────────────────────────────────────────

const DETAIL_LABELS = {
  attacker: 'Attacker',
  victim:   'Victim',
  method:   'Method',
  flag:     'Flag',
  msg:      'Raw log',
  type:     'Event type',
};

const EventDetail = ({ ev }) => (
  <div className="event-detail">
    {Object.entries(DETAIL_LABELS).map(([key, label]) =>
      ev[key] ? (
        <div className="event-detail__row" key={key}>
          <span>{label}</span>
          <code>{ev[key]}</code>
        </div>
      ) : null,
    )}
    <div className="event-detail__row">
      <span>Time</span>
      <code>{new Date(ev.ts * 1000).toLocaleString()}</code>
    </div>
  </div>
);

const EventRow = ({ ev }) => {
  const [open, setOpen] = useState(false);
  const meta = TYPE_META[ev.type] || { label: ev.type.toUpperCase(), cls: '' };
  const Icon = meta.icon;
  return (
    <div className={`event-row ${meta.cls} ${open ? 'is-open' : ''}`}>
      <button
        type="button"
        className="event-row__summary"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="event-row__chevron">
          {open
            ? <ChevronDown size={11} strokeWidth={2.6} />
            : <ChevronRight size={11} strokeWidth={2.6} />}
        </span>
        <span className="event-row__time">{fmt(ev.ts)}</span>
        <span className="event-row__tag">
          {Icon && <Icon size={11} strokeWidth={2.4} />}
          {meta.label}
        </span>
        <span className="event-row__attacker">{ev.attacker}</span>
        <span className="event-row__msg">{ev.msg}</span>
      </button>
      {open && <EventDetail ev={ev} />}
    </div>
  );
};

const EventLog = ({ events, connected }) => (
  <div className="event-log">
    <div className="event-log__toolbar">
      <span className={`event-log__status ${connected ? 'is-live' : 'is-offline'}`}>
        {connected ? 'LIVE' : 'OFFLINE'}
      </span>
      <span className="event-log__count">{events.length} events</span>
    </div>
    {events.length === 0 ? (
      <div className="event-log__empty">
        {connected ? 'Waiting for bot activity…' : 'Start bot/server.py to stream events.'}
      </div>
    ) : (
      <div className="event-log__feed">
        {[...events].reverse().map((ev, i) => (
          <EventRow key={`${ev.ts}-${i}`} ev={ev} />
        ))}
      </div>
    )}
  </div>
);

// ── DetailsPanel content (moved here to share the panel shell) ─────────────

import { ChevronDown as ChevronDownIcon } from 'lucide-react';

const SECTIONS = ['Device', 'Commands', 'Network Policy', 'Env Vars', 'Labels'];

const EmptyState = ({ children = 'No data defined.' }) => (
  <div className="details-empty">{children}</div>
);

const KeyValueRows = ({ values }) => {
  const entries = Object.entries(values || {});
  if (entries.length === 0) return <EmptyState />;
  return (
    <div className="kv-list">
      {entries.map(([key, value]) => (
        <div className="kv-row" key={key}>
          <span>{key}</span>
          <code>{String(value)}</code>
        </div>
      ))}
    </div>
  );
};

const ValueList = ({ values }) => {
  if (!values || values.length === 0) return <EmptyState />;
  return (
    <div className="value-list">
      {values.map((value, index) => (
        <code key={`${typeof value === 'string' ? value : value.raw}-${index}`}>
          {typeof value === 'string' ? value : value.raw}
        </code>
      ))}
    </div>
  );
};

const Field = ({ label, value }) => {
  if (value === undefined || value === null || value === '' || (Array.isArray(value) && value.length === 0)) {
    return null;
  }
  return (
    <div className="details-field">
      <span>{label}</span>
      <code>{Array.isArray(value) ? value.join(', ') : String(value)}</code>
    </div>
  );
};

const Accordion = ({ title, isOpen, onToggle, children }) => (
  <section className="details-accordion">
    <button type="button" className="details-accordion__trigger" onClick={onToggle}>
      <span>{title}</span>
      <ChevronDownIcon size={16} className={isOpen ? 'is-open' : ''} aria-hidden="true" />
    </button>
    {isOpen && <div className="details-accordion__body">{children}</div>}
  </section>
);

const NodeDetail = ({ node }) => {
  const [openSections, setOpenSections] = useState(() => new Set(['Device', 'Network Policy']));
  const toggle = (s) =>
    setOpenSections((cur) => {
      const next = new Set(cur);
      next.has(s) ? next.delete(s) : next.add(s);
      return next;
    });

  if (!node) {
    return (
      <div className="details-panel__placeholder">
        <h2>Select a container</h2>
        <p>Device settings, commands, policies, environment variables, and labels appear here.</p>
      </div>
    );
  }

  return (
    <>
      <div className="details-panel__header">
        <div>
          <div className="details-panel__eyebrow">{node.kind || 'service'}</div>
          <h2>{node.serviceName}</h2>
        </div>
        <span>{node.teamName || 'Unassigned'}</span>
      </div>
      {SECTIONS.map((section) => (
        <Accordion key={section} title={section} isOpen={openSections.has(section)} onToggle={() => toggle(section)}>
          {section === 'Device' && (
            <>
              <Field label="Container" value={node.containerName} />
              <Field label="Hostname" value={node.hostname} />
              <Field label="Image" value={node.image} />
              <Field label="Build context" value={node.build?.context} />
              <Field label="Dockerfile" value={node.dockerfile?.path || node.build?.dockerfile} />
              <Field label="Base image" value={node.dockerfile?.metadata?.baseImages} />
              <Field label="IP address" value={node.ipAddress || node.subnet} />
              <Field label="Network" value={node.primaryNetwork} />
            </>
          )}
          {section === 'Commands' && (
            <>
              <Field label="Command" value={node.command} />
              <Field label="Entrypoint" value={node.entrypoint} />
              <Field label="Dockerfile CMD" value={node.dockerfile?.metadata?.command} />
              <Field label="Dockerfile ENTRYPOINT" value={node.dockerfile?.metadata?.entrypoint} />
              <Field label="Exposed ports" value={node.dockerfile?.metadata?.exposedPorts} />
              <Field label="RUN steps" value={node.dockerfile?.metadata?.runSteps} />
            </>
          )}
          {section === 'Network Policy' && (
            <>
              <Field label="Restart" value={node.restart} />
              <Field label="Privileged" value={node.privileged ? 'yes' : undefined} />
              <Field label="Capabilities" value={node.capAdd} />
              <Field label="Depends on" value={node.dependsOn} />
              <Field label="Links" value={node.links} />
              <Field label="Expose" value={node.expose} />
              <div className="details-subtitle">Ports</div>
              <ValueList values={node.ports} />
              <div className="details-subtitle">Networks</div>
              <ValueList
                values={(node.networks || []).map((n) =>
                  [n.name, n.ipv4Address || n.subnet].filter(Boolean).join(' - '),
                )}
              />
              <div className="details-subtitle">Volumes</div>
              <ValueList values={node.volumes} />
            </>
          )}
          {section === 'Env Vars' && <KeyValueRows values={node.environment} />}
          {section === 'Labels' && <KeyValueRows values={node.labels} />}
        </Accordion>
      ))}
    </>
  );
};

// ── RightPanel — the combined shell ───────────────────────────────────────

const TABS = [
  { id: 'container', label: 'Container Info' },
  { id: 'log',       label: 'Bot Attack Log' },
];

const RightPanel = ({ node, events, connected }) => {
  const [tab, setTab] = useState('container');

  return (
    <aside className="right-panel details-panel is-open">
      <div className="right-panel__tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`right-panel__tab ${tab === t.id ? 'is-active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="right-panel__body">
        {tab === 'container' && <NodeDetail node={node} />}
        {tab === 'log'       && <EventLog events={events} connected={connected} />}
      </div>
    </aside>
  );
};

export default RightPanel;
