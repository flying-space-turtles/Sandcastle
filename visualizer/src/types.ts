import type { Edge, Node } from 'reactflow';

export type Mode = 'editor' | 'yaml' | 'inspector' | 'monitor';

export type EventType = 'sqli' | 'cmdi' | 'path-traversal' | 'ssh' | 'http' | 'tcp';

export interface LiveEvent {
  id: string;
  type: EventType | string;
  src: string;
  dst: string;
  ts: number;
  port?: number | string;
  detail?: string;
  _received?: number;
}

export interface DockerPort {
  raw: string;
  published?: string;
  target?: string;
  protocol?: string;
  host?: string;
}

export interface DockerNetworkRef {
  name: string;
  aliases: string[];
  ipv4Address?: string;
  ipv6Address?: string;
  raw: Record<string, unknown>;
  subnet?: string;
  gateway?: string;
}

export interface DockerfileMetadata {
  baseImages: string[];
  exposedPorts: string[];
  env: string[];
  entrypoint?: string;
  command?: string;
  runSteps: number;
  rawInstructionCount: number;
}

export interface DockerfileInfo {
  path: string;
  metadata: DockerfileMetadata;
}

export interface BuildConfig {
  context: string;
  dockerfile: string;
  args: Record<string, string>;
  raw: unknown;
}

export type ServiceKind = 'gateway' | 'firewall' | 'database' | 'service' | 'server';

export interface ServiceDefinition {
  id: string;
  serviceName: string;
  containerName?: string;
  hostname?: string;
  image?: string;
  build?: BuildConfig;
  dockerfile?: DockerfileInfo;
  kind: ServiceKind;
  teamId?: string;
  teamName: string;
  networks: DockerNetworkRef[];
  primaryNetwork: string;
  ipAddress?: string;
  dependsOn: string[];
  links: string[];
  ports: DockerPort[];
  expose: string[];
  environment: Record<string, string>;
  labels: Record<string, string>;
  command?: string | string[];
  entrypoint?: string | string[];
  restart?: string;
  volumes: string[];
  capAdd: string[];
  privileged: boolean;
  raw: Record<string, unknown>;
}

export interface NetworkDefinition {
  name: string;
  driver: string;
  external: boolean;
  subnet?: string;
  gateway?: string;
  raw: Record<string, unknown>;
}

export interface ParsedCompose {
  version?: string;
  name?: string;
  networks: NetworkDefinition[];
  services: ServiceDefinition[];
  volumes: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export type RelationRole = 'ssh' | 'vuln' | 'database' | 'firewall' | 'other';

export interface MachineNodeData extends ServiceDefinition {
  relationRole: RelationRole;
  shortLabel: string;
  accentColor?: string;
  subnet?: string;
  isHovered?: boolean;
  isRelated?: boolean;
  isDimmed?: boolean;
}

export interface NetworkGroupData {
  name: string;
  driver: string;
  subnet?: string;
  gateway?: string;
  serviceCount: number;
  teamCount: number;
  color?: string;
  accentColor?: string;
}

export type TopologyNodeData = MachineNodeData | NetworkGroupData;

export interface TopologyEdgeData {
  kind?: string;
  label?: string;
  defaultVisible?: boolean;
  revealOnHover?: boolean;
  eventType?: string;
}

export interface Topology {
  parsed: ParsedCompose;
  nodes: Array<Node<TopologyNodeData>>;
  edges: Array<Edge<TopologyEdgeData>>;
  nodeDetailsById: Record<string, MachineNodeData>;
}