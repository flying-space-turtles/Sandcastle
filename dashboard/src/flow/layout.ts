import type { TeamView } from '../types'

const RADIUS = 320
const TEAM_GAP = 110 // distance between SSH and vuln nodes within a team

export interface NodePosition {
  x: number
  y: number
}

export function gameserverPosition(): NodePosition {
  return { x: 0, y: 0 }
}

/** Lay teams out evenly around the gameserver. */
export function teamCenter(index: number, total: number): NodePosition {
  if (total === 0) return { x: 0, y: 0 }
  const angle = (2 * Math.PI * index) / total - Math.PI / 2
  return {
    x: Math.cos(angle) * RADIUS,
    y: Math.sin(angle) * RADIUS,
  }
}

/** Place SSH and vulnerable-service nodes around the team's center. */
export function teamSubNodes(_team: TeamView, index: number, total: number) {
  const center = teamCenter(index, total)
  const angle = (2 * Math.PI * index) / total - Math.PI / 2
  const tx = Math.cos(angle)
  const ty = Math.sin(angle)
  // SSH on the inner side (closer to gameserver), vuln on the outer side.
  return {
    ssh: { x: center.x - tx * (TEAM_GAP / 2), y: center.y - ty * (TEAM_GAP / 2) },
    vuln: { x: center.x + tx * (TEAM_GAP / 2), y: center.y + ty * (TEAM_GAP / 2) },
    label: { x: center.x + tx * (TEAM_GAP * 1.4), y: center.y + ty * (TEAM_GAP * 1.4) },
  }
}
