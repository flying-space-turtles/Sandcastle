import { memo } from 'react';

const CustomNetworkGroup = ({ data }) => (
  <div className="network-group" style={{ '--group-color': data.color || '#38bdf8' }}>
    <div className="network-group__header">
      <div>
        <div className="network-group__eyebrow">Network Zone</div>
        <div className="network-group__name">{data.name}</div>
      </div>
      <div className="network-group__count">
        {data.serviceCount} {data.serviceCount === 1 ? 'service' : 'services'}
      </div>
    </div>
    <div className="network-group__details">
      <span>{data.driver || 'default'} driver</span>
      {data.teamCount > 0 && <span>{data.teamCount} teams</span>}
      {data.subnet && <span>{data.subnet}</span>}
      {data.gateway && <span>gateway {data.gateway}</span>}
    </div>
  </div>
);

export default memo(CustomNetworkGroup);
