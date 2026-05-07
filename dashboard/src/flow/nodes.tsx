import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import type { SLAStatus, TeamView } from '../types'

const SLA_COLORS: Record<NonNullable<SLAStatus>, string> = {
  UP: '#22c55e',
  MUMBLE: '#eab308',
  CORRUPT: '#f97316',
  DOWN: '#ef4444',
}

function slaColor(status: SLAStatus): string {
  if (!status) return '#6b7280'
  return SLA_COLORS[status]
}

export type GameserverNodeData = {
  round: number
  paused: boolean
  numTeams: number
  tickDuration: number
  dockerAvailable: boolean
}

export type GameserverNodeType = Node<GameserverNodeData, 'gameserver'>

export function GameserverNode({ data }: NodeProps<GameserverNodeType>) {
  return (
    <div className={`gs-node ${data.paused ? 'gs-node--paused' : ''}`}>
      <div className="gs-node__title">GAMESERVER</div>
      <div className="gs-node__ip">10.10.0.2</div>
      <div className="gs-node__round">Round {data.round}</div>
      <div className="gs-node__meta">
        <span>{data.numTeams} teams</span>
        <span>·</span>
        <span>{data.tickDuration}s tick</span>
      </div>
      <div className={`gs-node__status ${data.paused ? 'is-paused' : 'is-running'}`}>
        {data.paused ? 'PAUSED' : 'RUNNING'}
      </div>
      {!data.dockerAvailable && (
        <div className="gs-node__warn">no docker socket</div>
      )}
      <Handle type="source" position={Position.Top} style={{ visibility: 'hidden' }} />
      <Handle type="target" position={Position.Top} style={{ visibility: 'hidden' }} />
    </div>
  )
}

export type TeamNodeData = {
  team: TeamView
  selected: boolean
  onSelect: (teamId: number) => void
}

export type SshNodeType = Node<TeamNodeData, 'ssh'>
export type VulnNodeType = Node<TeamNodeData, 'vuln'>

export function SshNode({ data }: NodeProps<SshNodeType>) {
  const t = data.team
  return (
    <div
      className={`team-node team-node--ssh ${data.selected ? 'is-selected' : ''}`}
      onClick={() => data.onSelect(t.id)}
    >
      <div className="team-node__head">
        <span className="team-node__icon">SSH</span>
        <span className="team-node__name">{t.name}</span>
      </div>
      <div className="team-node__sub">{`10.10.${t.id}.2`}</div>
      <div className="team-node__hint">port {2200 + t.id}</div>
      <Handle type="source" position={Position.Left} style={{ visibility: 'hidden' }} />
      <Handle type="target" position={Position.Left} style={{ visibility: 'hidden' }} />
    </div>
  )
}

export function VulnNode({ data }: NodeProps<VulnNodeType>) {
  const t = data.team
  const c = slaColor(t.sla_status)
  const stateClass = t.container.vuln_state === 'running' ? 'is-running' : 'is-down'
  return (
    <div
      className={`team-node team-node--vuln ${stateClass} ${data.selected ? 'is-selected' : ''}`}
      style={{ borderColor: c, boxShadow: `0 0 24px ${c}33` }}
      onClick={() => data.onSelect(t.id)}
    >
      <div className="team-node__head">
        <span
          className="team-node__dot"
          style={{ background: c, boxShadow: `0 0 12px ${c}` }}
        />
        <span className="team-node__name">{t.container.vuln}</span>
      </div>
      <div className="team-node__sub">{t.ip_address}</div>
      <div className="team-node__sla" style={{ color: c }}>
        SLA · {t.sla_status ?? 'unknown'}
      </div>
      <div className="team-node__detail">{t.sla_detail ?? '—'}</div>
      <Handle type="source" position={Position.Right} style={{ visibility: 'hidden' }} />
      <Handle type="target" position={Position.Left} style={{ visibility: 'hidden' }} />
    </div>
  )
}
