import { useMemo } from 'react'
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import type { GameserverState } from '../types'
import { gameserverPosition, teamSubNodes } from './layout'
import {
  GameserverNode,
  SshNode,
  VulnNode,
  type GameserverNodeData,
  type TeamNodeData,
} from './nodes'

const NODE_TYPES = {
  gameserver: GameserverNode,
  ssh: SshNode,
  vuln: VulnNode,
}

interface PolygonFlowProps {
  state: GameserverState | null
  selectedTeam: number | null
  onSelectTeam: (teamId: number) => void
  flashTeams: Set<number>
}

export function PolygonFlow({
  state,
  selectedTeam,
  onSelectTeam,
  flashTeams,
}: PolygonFlowProps) {
  const { nodes, edges } = useMemo(() => {
    if (!state) return { nodes: [] as Node[], edges: [] as Edge[] }

    const total = state.teams.length
    const gsPos = gameserverPosition()

    const gsData: GameserverNodeData = {
      round: state.round,
      paused: state.paused,
      numTeams: state.config.num_teams,
      tickDuration: state.config.tick_duration,
      dockerAvailable: state.docker.available,
    }

    const nodes: Node[] = [
      {
        id: 'gameserver',
        type: 'gameserver',
        position: gsPos,
        data: gsData,
        draggable: false,
        selectable: false,
      },
    ]

    const edges: Edge[] = []

    state.teams.forEach((team, i) => {
      const subs = teamSubNodes(team, i, total)
      const teamData: TeamNodeData = {
        team,
        selected: selectedTeam === team.id,
        onSelect: onSelectTeam,
      }

      nodes.push({
        id: `ssh-${team.id}`,
        type: 'ssh',
        position: subs.ssh,
        data: teamData,
        draggable: false,
      })
      nodes.push({
        id: `vuln-${team.id}`,
        type: 'vuln',
        position: subs.vuln,
        data: teamData,
        draggable: false,
      })

      // Gameserver → vuln-service link (used to plant flags & run SLA checks)
      const flashing = flashTeams.has(team.id)
      const slaColor =
        team.sla_status === 'UP'
          ? '#22c55e'
          : team.sla_status === 'DOWN'
            ? '#ef4444'
            : team.sla_status === 'CORRUPT'
              ? '#f97316'
              : team.sla_status === 'MUMBLE'
                ? '#eab308'
                : '#6b7280'

      edges.push({
        id: `gs-${team.id}-vuln`,
        source: 'gameserver',
        target: `vuln-${team.id}`,
        animated: flashing,
        style: {
          stroke: slaColor,
          strokeWidth: flashing ? 3 : 1.5,
          strokeDasharray: flashing ? '4 2' : undefined,
        },
        type: 'smoothstep',
      })
      // SSH → vuln-service (team manages their service from SSH)
      edges.push({
        id: `ssh-${team.id}-vuln`,
        source: `ssh-${team.id}`,
        target: `vuln-${team.id}`,
        style: { stroke: '#475569', strokeWidth: 1, strokeDasharray: '2 4' },
        type: 'straight',
      })
    })

    // Cross-team attack edges (any team can scrape any other team's IDOR)
    for (const from of state.teams) {
      for (const to of state.teams) {
        if (from.id === to.id) continue
        edges.push({
          id: `attack-${from.id}-${to.id}`,
          source: `vuln-${from.id}`,
          target: `vuln-${to.id}`,
          style: { stroke: '#1e293b', strokeWidth: 0.5, opacity: 0.4 },
          type: 'straight',
          animated: false,
        })
      }
    }

    return { nodes, edges }
  }, [state, selectedTeam, flashTeams, onSelectTeam])

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      fitView
      proOptions={{ hideAttribution: true }}
      colorMode="dark"
      nodesConnectable={false}
      nodesDraggable={false}
      edgesFocusable={false}
    >
      <Background gap={32} size={1} color="#1e293b" />
      <MiniMap pannable zoomable nodeStrokeWidth={2} />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}
