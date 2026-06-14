import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { firewallWsUrl } from '../data/arenaConfig';
import type { LiveEvent } from '../types';

const MAX_EVENTS = 200;
const MAX_LIVE_ROUTES = 24;
const LIVE_WINDOW_MS = 8000;

// Higher number = more severe (determines which event type wins for a src->dst pair)
const SEVERITY: Record<string, number> = { ssh: 8, telnet: 7, ftp: 6, smtp: 5, mysql: 4, postgres: 4, redis: 4, dns: 3, http: 3, udp: 2, icmp: 2, tcp: 0 };
const severityOf = (type: string) => SEVERITY[type] ?? 0;

const buildEvent = (raw: Record<string, unknown>): LiveEvent => {
  const src = typeof raw.src === 'string' ? raw.src : 'unknown';
  const dst = typeof raw.dst === 'string' ? raw.dst : 'unknown';
  const type = typeof raw.type === 'string' ? raw.type : 'tcp';
  const ts = typeof raw.ts === 'number' ? raw.ts : Math.floor(Date.now() / 1000);
  const id = typeof raw.id === 'string' ? raw.id : `${src}-${dst}-${type}-${ts}`;
  const port = typeof raw.port === 'number' || typeof raw.port === 'string' ? raw.port : undefined;
  const detail = typeof raw.detail === 'string' ? raw.detail : undefined;
  const maskedSrcIp = typeof raw.maskedSrcIp === 'string' ? raw.maskedSrcIp : undefined;

  return {
    id,
    src,
    dst,
    type,
    ts,
    port,
    detail,
    maskedSrcIp,
    _received: Date.now(),
  };
};

export function useNetworkEvents() {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [connected, setConnected] = useState(false);
  // Increment so liveEdges ages out stale entries even without new events.
  const [tick, setTick] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    const ws = new WebSocket(firewallWsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      if (wsRef.current !== ws) return;
      setConnected(true);
    };

    ws.onclose = () => {
      if (wsRef.current !== ws) return;
      setConnected(false);
      reconnectRef.current = setTimeout(connect, 3000);
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (e) => {
      if (wsRef.current !== ws) return;
      try {
        const parsed = JSON.parse(e.data) as Record<string, unknown>;
        const event = buildEvent(parsed);
        setEvents((prev) => [event, ...prev].slice(0, MAX_EVENTS));
      } catch {
        // ignore malformed messages
      }
    };
  }, []);

  useEffect(() => {
    connect();
    const ticker = setInterval(() => setTick((n) => n + 1), 2000);
    return () => {
      clearInterval(ticker);
      if (reconnectRef.current) {
        clearTimeout(reconnectRef.current);
      }
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close();
    };
  }, [connect]);

  // Derive unique src->dst live edges: keep the newest high-severity event per
  // pair within the short browser receive-time window, then cap canvas routes.
  const liveEdges = useMemo<LiveEvent[]>(() => {
    const now = Date.now();
    const pairs = new Map<string, LiveEvent>();
    for (const event of events) {
      const receivedAt = event._received ?? event.ts * 1000;
      if (now - receivedAt > LIVE_WINDOW_MS) {
        continue;
      }
      const key = `${event.src}||${event.dst}`;
      const existing = pairs.get(key);
      if (
        !existing ||
        severityOf(event.type) > severityOf(existing.type) ||
        (severityOf(event.type) === severityOf(existing.type) && receivedAt > (existing._received ?? existing.ts * 1000))
      ) {
        pairs.set(key, event);
      }
    }
    return [...pairs.values()]
      .sort((a, b) => (b._received ?? b.ts * 1000) - (a._received ?? a.ts * 1000))
      .slice(0, MAX_LIVE_ROUTES);
  }, [events, tick]);

  return { events, connected, liveEdges };
}
