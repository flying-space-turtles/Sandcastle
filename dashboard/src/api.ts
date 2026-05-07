import type { GameserverState } from './types'

const BASE = '' // proxied via Vite to the gameserver

async function send<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status} ${detail}`)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  state: () => send<GameserverState>('/api/state'),
  forceTick: () => send<{ status: string; detail?: string }>('/api/admin/tick', { method: 'POST' }),
  pause: () => send<{ status: string }>('/api/admin/pause', { method: 'POST' }),
  resume: () => send<{ status: string }>('/api/admin/resume', { method: 'POST' }),
  takeDown: (teamId: number) =>
    send<{ status: string; detail?: string }>(`/api/admin/team/${teamId}/down`, { method: 'POST' }),
  bringUp: (teamId: number) =>
    send<{ status: string; detail?: string }>(`/api/admin/team/${teamId}/up`, { method: 'POST' }),
  restart: (teamId: number) =>
    send<{ status: string; detail?: string }>(`/api/admin/team/${teamId}/restart`, { method: 'POST' }),
  submitFlag: (teamToken: string, flag: string) =>
    send<{ status: string; round: number }>('/api/submit', {
      method: 'POST',
      body: JSON.stringify({ team_token: teamToken, flag }),
    }),
}
