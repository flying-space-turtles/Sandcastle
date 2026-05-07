export type SLAStatus = 'UP' | 'DOWN' | 'MUMBLE' | 'CORRUPT' | null

export interface TeamView {
  id: number
  name: string
  ip_address: string
  service_url: string
  container: {
    ssh: string
    vuln: string
    vuln_state: string
  }
  latest_flag_round: number | null
  sla_status: SLAStatus
  sla_detail: string | null
  submission_token: string
}

export interface ScoreboardRow {
  team_id: number
  name: string
  attack: number
  defense: number
  sla: number
  total: number
}

export interface EventRow {
  id: number
  created_at: number
  round: number | null
  kind: string
  team_id: number | null
  message: string
}

export interface GameserverState {
  config: {
    num_teams: number
    tick_duration: number
    flag_expiry_rounds: number
  }
  round: number
  paused: boolean
  last_tick_at: number
  now: number
  docker: {
    available: boolean
    detail: string
  }
  teams: TeamView[]
  scoreboard: ScoreboardRow[]
  events: EventRow[]
}
