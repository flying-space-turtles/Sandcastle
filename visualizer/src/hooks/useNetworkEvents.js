import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

const WS_URL = 'ws://localhost:6789';
const MAX_EVENTS = 200;
const LIVE_WINDOW_SEC = 30;

// Higher number = more severe (determines which event type wins for a src→dst pair)
const SEVERITY = { sqli: 5, cmdi: 4, 'path-traversal': 3, ssh: 2, http: 1, tcp: 0 };
const severityOf = (type) => SEVERITY[type] ?? 0;

export function useNetworkEvents() {
  const [events, setEvents] = useState([]);
  const [connected, setConnected] = useState(false);
  // Increment every 5s so liveEdges ages out stale entries even without new events
  const [tick, setTick] = useState(0);
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);

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
        const event = { ...JSON.parse(e.data), _received: Date.now() };
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
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  // Derive unique src→dst live edges: keep the highest-severity event per pair
  // within the last LIVE_WINDOW_SEC seconds. Re-evaluated on new events and on
  // each 5-second tick so stale edges disappear automatically.
  const liveEdges = useMemo(() => {
    void tick;
    const now = Date.now() / 1000;
    const pairs = new Map();
    for (const e of events) {
      if (now - e.ts > LIVE_WINDOW_SEC) continue;
      const key = `${e.src}||${e.dst}`;
      const existing = pairs.get(key);
      if (!existing || severityOf(e.type) > severityOf(existing.type)) {
        pairs.set(key, e);
      }
    }
    return [...pairs.values()];
  }, [events, tick]);

  return { events, connected, liveEdges };
}
