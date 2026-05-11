import { memo, type CSSProperties } from 'react';
import { Handle, Position, type NodeProps } from 'reactflow';
import { Container, Database, Globe2, Router, Server, Shield } from 'lucide-react';
import type { MachineNodeData } from '../types';

const ICONS: Record<string, typeof Globe2> = {
  database: Database,
  firewall: Shield,
  gateway: Router,
  service: Container,
  server: Server,
};

const getNetworkLabel = (data: MachineNodeData) => {
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

const CustomMachineNode = ({ data, selected }: NodeProps<MachineNodeData>) => {
  const Icon = ICONS[data.kind] || Globe2;

  return (
    <div
      className={[
        'machine-node',
        `machine-node--${data.relationRole || data.kind || 'server'}`,
        selected ? 'is-selected' : '',
        data.isHovered ? 'is-hovered' : '',
        data.isRelated ? 'is-related' : '',
        data.isDimmed ? 'is-dimmed' : '',
      ]
        .filter(Boolean)
        .join(' ')}
      style={{ '--node-accent': data.accentColor || '#38bdf8' } as CSSProperties}
    >
      <Handle id="left" type="target" position={Position.Left} className="machine-node__handle" />
      <Handle id="top" type="target" position={Position.Top} className="machine-node__handle" />
      <div className="machine-node__header">
        <div className="machine-node__badge" aria-hidden="true">
          <span>{data.shortLabel}</span>
          <Icon size={13} strokeWidth={2.2} />
        </div>
        <div className="machine-node__identity">
          <div className="machine-node__name" title={data.serviceName}>
            {data.serviceName}
          </div>
          <div className="machine-node__kind">
            {data.relationRole === 'ssh'
              ? 'SSH container'
              : data.relationRole === 'vuln'
                ? 'Vulnerable app'
                : data.kind || 'service'}
          </div>
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

      <Handle id="bottom" type="source" position={Position.Bottom} className="machine-node__handle" />
      <Handle id="right" type="source" position={Position.Right} className="machine-node__handle" />
    </div>
  );
};

export default memo(CustomMachineNode);