import { Activity, Bot, Boxes, Brain, RadioTower, Shield, Trophy } from 'lucide-react';
import type { Mode } from '../types';

const MODES = [
  { id: 'scoreboard' as Mode, label: 'Match', icon: Trophy },
  { id: 'topology' as Mode, label: 'Arena', icon: Boxes },
  { id: 'firewall' as Mode, label: 'Traffic', icon: RadioTower },
  { id: 'bot' as Mode, label: 'Bots', icon: Bot },
  { id: 'agents' as Mode, label: 'Agents', icon: Brain },
];

const countLabel = (count: number, singular: string, plural = `${singular}s`) =>
  `${count} ${count === 1 ? singular : plural}`;

type TopologyNavProps = {
  mode: Mode;
  onModeChange: (mode: Mode) => void;
  serviceCount: number;
  networkCount: number;
  edgeCount: number;
  firewallConnected: boolean;
  firewallEventCount: number;
};

const TopologyNav = ({
  mode,
  onModeChange,
  serviceCount,
  networkCount,
  edgeCount,
  firewallConnected,
  firewallEventCount,
}: TopologyNavProps) => {
  const active = MODES.find((item) => item.id === mode) || MODES[0];
  return (
    <>
      <aside className="ops-rail">
        <div className="ops-rail__brand" title="Sandcastle">
          <Shield size={22} />
        </div>
        <nav className="ops-rail__nav" aria-label="Primary navigation">
          {MODES.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={[
                  mode === item.id ? 'is-active' : '',
                  item.id === 'firewall' && firewallConnected ? 'is-live' : '',
                ].filter(Boolean).join(' ')}
                type="button"
                onClick={() => onModeChange(item.id)}
                aria-current={mode === item.id ? 'page' : undefined}
              >
                <Icon size={19} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <div className={`ops-rail__health ${firewallConnected ? 'is-live' : ''}`} title="Firewall feed">
          <Activity size={16} />
        </div>
      </aside>
      <header className="ops-header">
        <div>
          <span>Sandcastle</span>
          <strong>{active.label}</strong>
        </div>
        <div className="ops-header__context">
          {mode === 'topology' && (
            <span>
              {countLabel(networkCount, 'network')} · {countLabel(serviceCount, 'service')} ·{' '}
              {countLabel(edgeCount, 'route')}
            </span>
          )}
          {mode === 'firewall' && (
            <span className={firewallConnected ? 'is-live' : ''}>
              {firewallConnected ? `${firewallEventCount} observed events` : 'Firewall feed offline'}
            </span>
          )}
          {mode === 'scoreboard' && <span>Authoritative match operations</span>}
          {mode === 'bot' && <span>Automated team deployments</span>}
          {mode === 'agents' && <span>AI agent runs — select provider and model</span>}
        </div>
      </header>
    </>
  );
};

export default TopologyNav;
