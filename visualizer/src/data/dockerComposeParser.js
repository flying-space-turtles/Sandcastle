import { parseDocument } from 'yaml';

const TEAM_LABEL_KEYS = [
  'team',
  'sandcastle.team',
  'com.sandcastle.team',
  'io.sandcastle.team',
];

const firstDefined = (...values) =>
  values.find((value) => value !== undefined && value !== null && value !== '');

const toArray = (value) => {
  if (value === undefined || value === null) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
};

const splitKeyValue = (entry) => {
  const separator = String(entry).indexOf('=');
  if (separator === -1) {
    return [String(entry), ''];
  }
  return [String(entry).slice(0, separator), String(entry).slice(separator + 1)];
};

const normalizeKeyValue = (value) => {
  if (!value) {
    return {};
  }

  if (Array.isArray(value)) {
    return value.reduce((acc, entry) => {
      const [key, entryValue] = splitKeyValue(entry);
      acc[key] = entryValue;
      return acc;
    }, {});
  }

  if (typeof value === 'object') {
    return Object.entries(value).reduce((acc, [key, entryValue]) => {
      acc[key] = entryValue === null || entryValue === undefined ? '' : String(entryValue);
      return acc;
    }, {});
  }

  return {};
};

const normalizeDependsOn = (value) => {
  if (!value) {
    return [];
  }

  if (Array.isArray(value)) {
    return value.map(String);
  }

  if (typeof value === 'object') {
    return Object.keys(value);
  }

  return [String(value)];
};

const normalizeLinks = (value) =>
  toArray(value)
    .map((link) => String(link).split(':')[0])
    .filter(Boolean);

const normalizePorts = (value) =>
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
      return {
        raw: JSON.stringify(port),
        published: port.published,
        target: port.target,
        protocol: port.protocol,
        host: port.host_ip,
      };
    }

    return {
      raw: String(port),
    };
  });

const normalizeNetworkRefs = (serviceNetworks, knownNetworks) => {
  if (!serviceNetworks) {
    return knownNetworks.default
      ? [{ name: 'default', config: {} }]
      : [];
  }

  if (Array.isArray(serviceNetworks)) {
    return serviceNetworks.map((name) => ({
      name: String(name),
      config: {},
    }));
  }

  if (typeof serviceNetworks === 'object') {
    return Object.entries(serviceNetworks).map(([name, config]) => ({
      name,
      config: config && typeof config === 'object' ? config : {},
    }));
  }

  return [];
};

const normalizeNetworks = (networks = {}) =>
  Object.entries(networks).map(([name, config = {}]) => {
    const ipamConfig = Array.isArray(config?.ipam?.config) ? config.ipam.config[0] : undefined;

    return {
      name,
      driver: config?.driver || 'default',
      external: Boolean(config?.external),
      subnet: firstDefined(ipamConfig?.subnet, config?.subnet),
      gateway: firstDefined(ipamConfig?.gateway, config?.gateway),
      raw: config || {},
    };
  });

const normalizeBuild = (build) => {
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

  const context = build.context || '.';
  const dockerfile = build.dockerfile || 'Dockerfile';

  return {
    context,
    dockerfile,
    args: normalizeKeyValue(build.args),
    raw: build,
  };
};

export const parseDockerfile = (source = '') => {
  const instructions = [];

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

const resolveDockerfileSource = (build, dockerfileSources) => {
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

const inferTeamId = (serviceName, environment, labels, buildArgs) => {
  const explicit = firstDefined(environment.TEAM_ID, labels.TEAM_ID, buildArgs.TEAM_ID);
  if (explicit) {
    return String(explicit);
  }

  const match = serviceName.match(/team[-_]?(\d+)/i);
  return match ? match[1] : undefined;
};

const inferTeamName = (serviceName, environment, labels, buildArgs) => {
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

const inferServiceKind = (serviceName, serviceConfig) => {
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

export const parseDockerCompose = (yamlSource, dockerfileSources = {}) => {
  const document = parseDocument(yamlSource || '');

  if (document.errors.length > 0) {
    throw new Error(document.errors.map((error) => error.message).join('\n'));
  }

  const compose = document.toJS({ mapAsMap: false }) || {};
  const networkList = normalizeNetworks(compose.networks || {});
  const knownNetworks = networkList.reduce((acc, network) => {
    acc[network.name] = network;
    return acc;
  }, {});

  const services = Object.entries(compose.services || {}).map(([serviceName, serviceConfig = {}]) => {
    const environment = normalizeKeyValue(serviceConfig.environment);
    const labels = normalizeKeyValue(serviceConfig.labels);
    const build = normalizeBuild(serviceConfig.build);
    const buildArgs = build?.args || {};
    const networkRefs = normalizeNetworkRefs(serviceConfig.networks, knownNetworks).map((network) => ({
      name: network.name,
      aliases: toArray(network.config.aliases).map(String),
      ipv4Address: network.config.ipv4_address,
      ipv6Address: network.config.ipv6_address,
      raw: network.config,
      subnet: knownNetworks[network.name]?.subnet,
      gateway: knownNetworks[network.name]?.gateway,
    }));
    const dockerfile = resolveDockerfileSource(build, dockerfileSources);
    const teamId = inferTeamId(serviceName, environment, labels, buildArgs);

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
      command: serviceConfig.command,
      entrypoint: serviceConfig.entrypoint,
      restart: serviceConfig.restart,
      volumes: toArray(serviceConfig.volumes).map(String),
      capAdd: toArray(serviceConfig.cap_add).map(String),
      privileged: Boolean(serviceConfig.privileged),
      raw: serviceConfig,
    };
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
