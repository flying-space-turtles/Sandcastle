import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { LiveEvent } from '../types';

const WS_URL = 'ws://localhost:6789';
const MAX_EVENTS = 200;
const LIVE_WINDOW_SEC = 30;

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
  // Increment every 5s so liveEdges ages out stale entries even without new events
  const [tick, setTick] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);

    ws.onclose = () => {
      setConnected(false);
      reconnectRef.current = setTimeout(connect, 3000);
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (e) => {
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
    const ticker = setInterval(() => setTick((n) => n + 1), 5000);
    return () => {
      clearInterval(ticker);
      if (reconnectRef.current) {
        clearTimeout(reconnectRef.current);
      }
      wsRef.current?.close();
    };
  }, [connect]);

  // Derive unique src->dst live edges: keep the highest-severity event per pair
  // within the last LIVE_WINDOW_SEC seconds. Re-evaluated on new events and on
  // each 5-second tick so stale edges disappear automatically.
  const liveEdges = useMemo<LiveEvent[]>(() => {
    const now = Date.now() / 1000;
    const pairs = new Map<string, LiveEvent>();
    for (const event of events) {
      if (now - event.ts > LIVE_WINDOW_SEC) {
        continue;
      }
      const key = `${event.src}||${event.dst}`;
      const existing = pairs.get(key);
      if (!existing || severityOf(event.type) > severityOf(existing.type)) {
        pairs.set(key, event);
      }
    }
    return [...pairs.values()];
  }, [events, tick]);

  return { events, connected, liveEdges };
}
