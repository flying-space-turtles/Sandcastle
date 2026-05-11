import { ChevronDown } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import type { MachineNodeData } from '../types';

const SECTIONS = ['Device', 'Commands', 'Network Policy', 'Env Vars', 'Labels'] as const;
type Section = (typeof SECTIONS)[number];

const EmptyState = ({ children = 'No data defined.' }: { children?: ReactNode }) => (
  <div className="details-empty">{children}</div>
);

const KeyValueRows = ({ values }: { values?: Record<string, string> }) => {
  const entries = Object.entries(values || {});

  if (entries.length === 0) {
    return <EmptyState />;
  }

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

type ValueEntry = string | { raw: string };

const ValueList = ({ values }: { values?: ValueEntry[] }) => {
  if (!values || values.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className="value-list">
      {values.map((value, index) => {
        const rendered = typeof value === 'string' ? value : value.raw;
        return (
          <code key={`${rendered}-${index}`}>
            {rendered}
          </code>
        );
      })}
    </div>
  );
};

type FieldValue = string | number | boolean | Array<string | number | boolean> | null | undefined;

const Field = ({ label, value }: { label: string; value: FieldValue }) => {
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

const Accordion = ({
  title,
  isOpen,
  onToggle,
  children,
}: {
  title: string;
  isOpen: boolean;
  onToggle: () => void;
  children: ReactNode;
}) => (
  <section className="details-accordion">
    <button type="button" className="details-accordion__trigger" onClick={onToggle}>
      <span>{title}</span>
      <ChevronDown size={16} className={isOpen ? 'is-open' : ''} aria-hidden="true" />
    </button>
    {isOpen && <div className="details-accordion__body">{children}</div>}
  </section>
);

const DetailsPanel = ({ node }: { node: MachineNodeData | null }) => {
  const [openSections, setOpenSections] = useState<Set<Section>>(
    () => new Set<Section>(['Device', 'Network Policy']),
  );

  const toggleSection = (section: Section) => {
    setOpenSections((current) => {
      const next = new Set(current);
      if (next.has(section)) {
        next.delete(section);
      } else {
        next.add(section);
      }
      return next;
    });
  };

  return (
    <aside className={`details-panel ${node ? 'is-open' : ''}`}>
      {node ? (
        <>
          <div className="details-panel__header">
            <div>
              <div className="details-panel__eyebrow">{node.kind || 'service'}</div>
              <h2>{node.serviceName}</h2>
            </div>
            <span>{node.teamName || 'Unassigned'}</span>
          </div>

          {SECTIONS.map((section) => (
            <Accordion
              key={section}
              title={section}
              isOpen={openSections.has(section)}
              onToggle={() => toggleSection(section)}
            >
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
                  <Field label="Command" value={node.command as FieldValue} />
                  <Field label="Entrypoint" value={node.entrypoint as FieldValue} />
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
                    values={(node.networks || []).map((network) =>
                      [network.name, network.ipv4Address || network.subnet].filter(Boolean).join(' - '),
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
      ) : (
        <div className="details-panel__placeholder">
          <h2>Select a machine</h2>
          <p>Device settings, commands, policies, environment variables, and labels appear here.</p>
        </div>
      )}
    </aside>
  );
};

export default DetailsPanel;