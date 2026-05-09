import { memo } from 'react';
import { Handle, Position } from 'reactflow';
import { Container, Database, Globe2, Router, Server, Shield } from 'lucide-react';

const ICONS = {
  database: Database,
  firewall: Shield,
  gateway: Router,
  service: Container,
  server: Server,
};

const getNetworkLabel = (data) => {
  if (data.ipAddress) {
    return data.ipAddress;
  }
  if (data.subnet) {
    return data.subnet;
  }
  if (data.networks?.[0]?.subnet) {
    return data.networks[0].subnet;
  }
  return 'dynamic address';
};

const CustomMachineNode = ({ data, selected }) => {
  const Icon = ICONS[data.kind] || Globe2;

  return (
    <div
      className={`machine-node machine-node--${data.kind || 'server'} ${selected ? 'is-selected' : ''}`}
      style={{ '--node-accent': data.accentColor || '#38bdf8' }}
    >
      <Handle type="target" position={Position.Left} className="machine-node__handle" />
      <div className="machine-node__header">
        <div className="machine-node__icon" aria-hidden="true">
          <Icon size={20} strokeWidth={2.2} />
        </div>
        <div className="machine-node__identity">
          <div className="machine-node__name" title={data.serviceName}>
            {data.serviceName}
          </div>
          <div className="machine-node__kind">{data.kind || 'service'}</div>
        </div>
      </div>

      <div className="machine-node__meta">
        <span>{data.teamName || 'Unassigned'}</span>
        <span>{getNetworkLabel(data)}</span>
      </div>

      {data.ports?.length > 0 && (
        <div className="machine-node__ports">
          {data.ports.slice(0, 2).map((port) => (
            <span key={port.raw}>{port.raw}</span>
          ))}
        </div>
      )}

      <Handle type="source" position={Position.Right} className="machine-node__handle" />
    </div>
  );
};

export default memo(CustomMachineNode);
