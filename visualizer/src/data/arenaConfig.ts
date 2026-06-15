import arenaEnv from '../../../config/arena.env?raw';

const values = arenaEnv.split(/\r?\n/).reduce<Record<string, string>>((acc, rawLine) => {
  const line = rawLine.trim();
  if (!line || line.startsWith('#')) {
    return acc;
  }
  const separator = line.indexOf('=');
  if (separator === -1) {
    throw new Error(`Invalid arena configuration line: ${rawLine}`);
  }
  acc[line.slice(0, separator)] = line.slice(separator + 1);
  return acc;
}, {});

const required = (key: string) => {
  const value = values[key];
  if (!value) {
    throw new Error(`Missing ${key} in config/arena.env`);
  }
  return value;
};

const requiredNumber = (key: string, minimum: number, maximum: number) => {
  const value = Number(required(key));
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${key} must be an integer between ${minimum} and ${maximum}`);
  }
  return value;
};

const subnet = required('ARENA_CTF_SUBNET');
const subnetMatch = subnet.match(/^(\d{1,3})\.(\d{1,3})\.0\.0\/16$/);
if (!subnetMatch) {
  throw new Error('ARENA_CTF_SUBNET must use the supported A.B.0.0/16 form');
}
if (subnetMatch.slice(1).some((octet) => Number(octet) > 255)) {
  throw new Error('ARENA_CTF_SUBNET contains an invalid octet');
}
const networkPrefix = `${Number(subnetMatch[1])}.${Number(subnetMatch[2])}`;

export const ARENA_CONFIG = {
  teamCount: requiredNumber('ARENA_TEAM_COUNT', 1, 250),
  servicePort: requiredNumber('ARENA_SERVICE_PORT', 1, 65535),
  sshBasePort: requiredNumber('ARENA_SSH_BASE_PORT', 1024, 65285),
  serviceIpPattern: `${networkPrefix}.{team}.3`,
  firewallWsPort: requiredNumber('ARENA_FIREWALL_WS_PORT', 1, 65535),
  gameserverPort: requiredNumber('ARENA_GAMESERVER_PORT', 1, 65535),
  botApiHost: required('ARENA_BOT_API_HOST'),
  botApiPort: requiredNumber('ARENA_BOT_API_PORT', 1, 65535),
  botLoopSeconds: requiredNumber('ARENA_BOT_LOOP_SECONDS', 0, 86400),
};

const cleanBaseUrl = (value: string | undefined) => (value || '').replace(/\/+$/, '');

const defaultFirewallWsUrl = () => {
  if (typeof window === 'undefined') {
    return '/firewall-ws';
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/firewall-ws`;
};

export const botApiUrl = cleanBaseUrl(import.meta.env.VITE_BOT_API_URL) || '/bot-api';
export const firewallWsUrl = cleanBaseUrl(import.meta.env.VITE_FIREWALL_WS_URL) || defaultFirewallWsUrl();
export const gameserverApiUrl = (import.meta.env.VITE_GAMESERVER_API_URL || '').replace(/\/+$/, '');
