const MODES = [
  { id: 'editor', label: 'Editor Mode' },
  { id: 'yaml', label: 'Yaml Mode' },
  { id: 'inspector', label: 'Inspector' },
  { id: 'firewall', label: 'Firewall' },
];

const TopologyNav = ({
  mode,
  onModeChange,
  serviceCount,
  networkCount,
  edgeCount,
  parseError,
  firewallConnected,
  firewallEventCount,
}) => (
  <header className="topology-nav">
    <div className="topology-nav__brand">
      <span className="topology-nav__mark" />
      <div>
        <div className="topology-nav__title">Docker Architecture Visualizer</div>
        <div className="topology-nav__subtitle">Sandcastle topology map</div>
      </div>
    </div>

    <nav className="topology-nav__modes" aria-label="Visualizer modes">
      {MODES.map((item) => (
        <button
          key={item.id}
          className={[
            mode === item.id ? 'is-active' : '',
            item.id === 'firewall' && firewallConnected ? 'is-firewall-live' : '',
          ]
            .filter(Boolean)
            .join(' ')}
          type="button"
          onClick={() => onModeChange(item.id)}
        >
          {item.label}
        </button>
      ))}
    </nav>

    <div className="topology-nav__stats">
      {parseError ? (
        <span className="topology-nav__error">YAML error</span>
      ) : (
        <>
          <span>{networkCount} networks</span>
          <span>{serviceCount} services</span>
          <span>{edgeCount} edges</span>
        </>
      )}
      <span className={`topology-nav__firewall-status ${firewallConnected ? 'is-live' : 'is-offline'}`}>
        {firewallConnected ? `● ${firewallEventCount} events` : '○ Firewall offline'}
      </span>
    </div>
  </header>
);

export default TopologyNav;
