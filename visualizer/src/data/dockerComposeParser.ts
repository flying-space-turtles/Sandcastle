import { parseDocument } from 'yaml';
import type {
  BuildConfig,
  DockerNetworkRef,
  DockerPort,
  DockerfileInfo,
  DockerfileMetadata,
  NetworkDefinition,
  ParsedCompose,
  ServiceDefinition,
} from '../types';

const TEAM_LABEL_KEYS = [
  'team',
  'sandcastle.team',
  'com.sandcastle.team',
  'io.sandcastle.team',
];

type RawService = {
  container_name?: string;
  hostname?: string;
  image?: string;
  build?: unknown;
  networks?: unknown;
  depends_on?: unknown;
  links?: unknown;
  ports?: unknown;
  expose?: unknown;
  environment?: unknown;
  labels?: unknown;
  command?: unknown;
  entrypoint?: unknown;
  restart?: unknown;
  volumes?: unknown;
  cap_add?: unknown;
  privileged?: unknown;
} & Record<string, unknown>;

type RawCompose = {
  version?: string;
  name?: string;
  networks?: Record<string, unknown>;
  services?: Record<string, RawService>;
  volumes?: Record<string, unknown>;
} & Record<string, unknown>;

type BuildLike = string | Record<string, unknown>;

const firstDefined = <T>(...values: Array<T | null | undefined | ''>): T | undefined =>
  values.find((value) => value !== undefined && value !== null && value !== '') as T | undefined;

const toArray = <T>(value: T | T[] | null | undefined): T[] => {
  if (value === undefined || value === null) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
};

const splitKeyValue = (entry: unknown): [string, string] => {
  const separator = String(entry).indexOf('=');
  if (separator === -1) {
    return [String(entry), ''];
  }
  return [String(entry).slice(0, separator), String(entry).slice(separator + 1)];
};

const normalizeKeyValue = (value: unknown): Record<string, string> => {
  if (!value) {
    return {};
  }

  if (Array.isArray(value)) {
    return value.reduce<Record<string, string>>((acc, entry) => {
      const [key, entryValue] = splitKeyValue(entry);
      acc[key] = entryValue;
      return acc;
    }, {});
  }

  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>).reduce<Record<string, string>>(
      (acc, [key, entryValue]) => {
        acc[key] = entryValue === null || entryValue === undefined ? '' : String(entryValue);
        return acc;
      },
      {},
    );
  }

  return {};
};

const normalizeDependsOn = (value: unknown): string[] => {
  if (!value) {
    return [];
  }

  if (Array.isArray(value)) {
    return value.map(String);
  }

  if (typeof value === 'object') {
    return Object.keys(value as Record<string, unknown>);
  }

  return [String(value)];
};

const normalizeLinks = (value: unknown): string[] =>
  toArray(value)
    .map((link) => String(link).split(':')[0])
    .filter(Boolean);

const normalizePorts = (value: unknown): DockerPort[] =>
  toArray(value).map((port) => {
    if (typeof port === 'string') {
      const parts = port.split(':');
      if (parts.length === 1) {
        return {
          raw: port,
          target: parts[0],
        };
      }

      return {
        raw: port,
        published: parts.length === 3 ? parts[1] : parts[0],
        target: parts[parts.length - 1],
        host: parts.length === 3 ? parts[0] : undefined,
      };
    }

    if (port && typeof port === 'object') {
      const portObject = port as Record<string, unknown>;
      const published = portObject.published;
      const target = portObject.target;
      return {
        raw: JSON.stringify(portObject),
        published: published === undefined ? undefined : String(published),
        target: target === undefined ? undefined : String(target),
        protocol: typeof portObject.protocol === 'string' ? portObject.protocol : undefined,
        host: typeof portObject.host_ip === 'string' ? portObject.host_ip : undefined,
      };
    }

    return {
      raw: String(port),
    };
  });

type NetworkRef = { name: string; config: Record<string, unknown> };

const normalizeNetworkRefs = (
  serviceNetworks: unknown,
  knownNetworks: Record<string, NetworkDefinition>,
): NetworkRef[] => {
  if (!serviceNetworks) {
    return knownNetworks.default ? [{ name: 'default', config: {} }] : [];
  }

  if (Array.isArray(serviceNetworks)) {
    return serviceNetworks.map((name) => ({
      name: String(name),
      config: {},
    }));
  }

  if (typeof serviceNetworks === 'object') {
    return Object.entries(serviceNetworks as Record<string, unknown>).map(([name, config]) => ({
      name,
      config: config && typeof config === 'object' ? (config as Record<string, unknown>) : {},
    }));
  }

  return [];
};

const normalizeNetworks = (networks: Record<string, unknown> = {}): NetworkDefinition[] =>
  Object.entries(networks).map(([name, config = {}]) => {
    const configObject = config as Record<string, unknown>;
    const ipamConfig = Array.isArray((configObject.ipam as Record<string, unknown> | undefined)?.config)
      ? ((configObject.ipam as Record<string, unknown>).config as Array<Record<string, unknown>>)[0]
      : undefined;

    return {
      name,
      driver: (configObject.driver as string) || 'default',
      external: Boolean(configObject.external),
      subnet: firstDefined(ipamConfig?.subnet as string | undefined, configObject.subnet as string | undefined),
      gateway: firstDefined(ipamConfig?.gateway as string | undefined, configObject.gateway as string | undefined),
      raw: configObject || {},
    };
  });

const normalizeBuild = (build: BuildLike | null | undefined): BuildConfig | undefined => {
  if (!build) {
    return undefined;
  }

  if (typeof build === 'string') {
    return {
      context: build,
      dockerfile: `${build.replace(/\/$/, '')}/Dockerfile`,
      args: {},
      raw: build,
    };
  }

  if (typeof build === 'object') {
    const buildConfig = build as Record<string, unknown>;
    const context = typeof buildConfig.context === 'string' ? buildConfig.context : '.';
    const dockerfile = typeof buildConfig.dockerfile === 'string' ? buildConfig.dockerfile : 'Dockerfile';

    return {
      context,
      dockerfile,
      args: normalizeKeyValue(buildConfig.args),
      raw: buildConfig,
    };
  }

  return undefined;
};

export const parseDockerfile = (source = ''): DockerfileMetadata => {
  const instructions: Array<{ instruction: string; value: string }> = [];

  source.split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) {
      return;
    }

    const match = trimmed.match(/^([A-Z]+)\s+(.*)$/i);
    if (!match) {
      return;
    }

    instructions.push({
      instruction: match[1].toUpperCase(),
      value: match[2],
    });
  });

  return {
    baseImages: instructions
      .filter((item) => item.instruction === 'FROM')
      .map((item) => item.value),
    exposedPorts: instructions
      .filter((item) => item.instruction === 'EXPOSE')
      .flatMap((item) => item.value.split(/\s+/))
      .filter(Boolean),
    env: instructions
      .filter((item) => item.instruction === 'ENV')
      .map((item) => item.value),
    entrypoint: instructions.find((item) => item.instruction === 'ENTRYPOINT')?.value,
    command: instructions.find((item) => item.instruction === 'CMD')?.value,
    runSteps: instructions.filter((item) => item.instruction === 'RUN').length,
    rawInstructionCount: instructions.length,
  };
};

const resolveDockerfileSource = (
  build: BuildConfig | undefined,
  dockerfileSources: Record<string, string>,
): DockerfileInfo | undefined => {
  if (!build?.dockerfile) {
    return undefined;
  }

  const candidates = [
    build.dockerfile,
    build.dockerfile.replace(/^\.\//, ''),
    `${build.context}/${build.dockerfile}`.replace(/^\.\//, ''),
  ];

  const key = candidates.find((candidate) => dockerfileSources[candidate]);
  if (!key) {
    return undefined;
  }

  return {
    path: key,
    metadata: parseDockerfile(dockerfileSources[key]),
  };
};

const inferTeamId = (
  serviceName: string,
  environment: Record<string, string>,
  labels: Record<string, string>,
  buildArgs: Record<string, string>,
) => {
  const explicit = firstDefined(environment.TEAM_ID, labels.TEAM_ID, buildArgs.TEAM_ID);
  if (explicit) {
    return String(explicit);
  }

  const match = serviceName.match(/team[-_]?(\d+)/i);
  return match ? match[1] : undefined;
};

const inferTeamName = (
  serviceName: string,
  environment: Record<string, string>,
  labels: Record<string, string>,
  buildArgs: Record<string, string>,
) => {
  const labelValue = TEAM_LABEL_KEYS.map((key) => labels[key]).find(Boolean);
  const explicit = firstDefined(
    labelValue,
    environment.TEAM_NAME,
    labels.TEAM_NAME,
    buildArgs.TEAM_NAME,
  );

  if (explicit) {
    return String(explicit).replace(/^team(\d+)$/i, 'Team $1');
  }

  const teamId = inferTeamId(serviceName, environment, labels, buildArgs);
  return teamId ? `Team ${teamId}` : 'Unassigned';
};

const inferServiceKind = (serviceName: string, serviceConfig: RawService): ServiceDefinition['kind'] => {
  const text = [
    serviceName,
    serviceConfig.image,
    serviceConfig.container_name,
    serviceConfig.hostname,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();

  if (/(ssh|gateway|jump|bastion)/.test(text)) {
    return 'gateway';
  }
  if (/(firewall|proxy|traefik|nginx|haproxy|waf)/.test(text)) {
    return 'firewall';
  }
  if (/(postgres|mysql|mariadb|mongo|redis|sqlite|database|db)/.test(text)) {
    return 'database';
  }
  if (/(vuln|app|api|web|http|service)/.test(text)) {
    return 'service';
  }
  return 'server';
};

export const parseDockerCompose = (
  yamlSource: string,
  dockerfileSources: Record<string, string> = {},
): ParsedCompose => {
  const document = parseDocument(yamlSource || '');

  if (document.errors.length > 0) {
    throw new Error(document.errors.map((error) => error.message).join('\n'));
  }

  const compose = (document.toJS({ mapAsMap: false }) || {}) as RawCompose;
  const networkList = normalizeNetworks((compose.networks || {}) as Record<string, unknown>);
  const knownNetworks = networkList.reduce<Record<string, NetworkDefinition>>((acc, network) => {
    acc[network.name] = network;
    return acc;
  }, {});

  const services = Object.entries(compose.services || {}).map(([serviceName, serviceConfig = {}]) => {
    const environment = normalizeKeyValue(serviceConfig.environment);
    const labels = normalizeKeyValue(serviceConfig.labels);
    const build = normalizeBuild(serviceConfig.build as BuildLike | undefined);
    const buildArgs = build?.args || {};
    const networkRefs = normalizeNetworkRefs(serviceConfig.networks, knownNetworks).map((network) => {
      const config = network.config as Record<string, unknown>;
      return {
        name: network.name,
        aliases: toArray(config.aliases).map(String),
        ipv4Address: typeof config.ipv4_address === 'string' ? config.ipv4_address : undefined,
        ipv6Address: typeof config.ipv6_address === 'string' ? config.ipv6_address : undefined,
        raw: config,
        subnet: knownNetworks[network.name]?.subnet,
        gateway: knownNetworks[network.name]?.gateway,
      } as DockerNetworkRef;
    });
    const dockerfile = resolveDockerfileSource(build, dockerfileSources);
    const teamId = inferTeamId(serviceName, environment, labels, buildArgs);
    const command =
      typeof serviceConfig.command === 'string' || Array.isArray(serviceConfig.command)
        ? (serviceConfig.command as string | string[])
        : undefined;
    const entrypoint =
      typeof serviceConfig.entrypoint === 'string' || Array.isArray(serviceConfig.entrypoint)
        ? (serviceConfig.entrypoint as string | string[])
        : undefined;

    return {
      id: serviceName,
      serviceName,
      containerName: serviceConfig.container_name,
      hostname: serviceConfig.hostname,
      image: serviceConfig.image,
      build,
      dockerfile,
      kind: inferServiceKind(serviceName, serviceConfig),
      teamId,
      teamName: inferTeamName(serviceName, environment, labels, buildArgs),
      networks: networkRefs,
      primaryNetwork: networkRefs[0]?.name || 'external',
      ipAddress: firstDefined(networkRefs[0]?.ipv4Address, networkRefs[0]?.ipv6Address),
      dependsOn: normalizeDependsOn(serviceConfig.depends_on),
      links: normalizeLinks(serviceConfig.links),
      ports: normalizePorts(serviceConfig.ports),
      expose: toArray(serviceConfig.expose).map(String),
      environment,
      labels,
      command,
      entrypoint,
      restart: typeof serviceConfig.restart === 'string' ? serviceConfig.restart : undefined,
      volumes: toArray(serviceConfig.volumes).map(String),
      capAdd: toArray(serviceConfig.cap_add).map(String),
      privileged: Boolean(serviceConfig.privileged),
      raw: serviceConfig,
    } as ServiceDefinition;
  });

  return {
    version: compose.version,
    name: compose.name,
    networks: networkList,
    services,
    volumes: compose.volumes || {},
    raw: compose,
  };
};