const GROUP_PALETTE = [
  '#38bdf8',
  '#a78bfa',
  '#34d399',
  '#f87171',
  '#f472b6',
  '#fbbf24',
];

const KIND_ORDER = {
  gateway: 0,
  firewall: 1,
  service: 2,
  server: 3,
  database: 4,
};

const MACHINE_WIDTH = 224;
const MACHINE_HEIGHT = 126;
const GROUP_PADDING_X = 42;
const GROUP_HEADER = 112;
const COLUMN_WIDTH = 268;
const ROW_HEIGHT = 154;
const GROUP_GAP = 110;
const GROUP_MAX_ROW_WIDTH = 1500;

const networkNodeId = (name) => `network:${name}`;
const gatewayNodeId = (name) => `gateway:${name}`;

const edgeKey = (source, target, kind) => `${source}->${target}:${kind}`;

const teamSortValue = (service) => {
  const numeric = Number.parseInt(service.teamId, 10);
  if (!Number.isNaN(numeric)) {
    return numeric;
  }
  return Number.MAX_SAFE_INTEGER;
};

const sortServices = (a, b) => {
  const teamDelta = teamSortValue(a) - teamSortValue(b);
  if (teamDelta !== 0) {
    return teamDelta;
  }

  const kindDelta = (KIND_ORDER[a.kind] ?? 99) - (KIND_ORDER[b.kind] ?? 99);
  if (kindDelta !== 0) {
    return kindDelta;
  }

  return a.serviceName.localeCompare(b.serviceName);
};

const getTeamKey = (service) =>
  service.teamId ? `team-${service.teamId}` : `${service.teamName}:${service.serviceName}`;

const buildNetworkCatalog = (parsed) => {
  const catalog = new Map(parsed.networks.map((network) => [network.name, network]));

  parsed.services.forEach((service) => {
    const networkName = service.primaryNetwork || 'external';
    if (!catalog.has(networkName)) {
      catalog.set(networkName, {
        name: networkName,
        driver: networkName === 'external' ? 'external' : 'default',
      });
    }
  });

  if (catalog.size === 0) {
    catalog.set('external', {
      name: 'external',
      driver: 'external',
    });
  }

  return [...catalog.values()];
};

const groupServicesByNetwork = (services) =>
  services.reduce((acc, service) => {
    const networkName = service.primaryNetwork || 'external';
    if (!acc.has(networkName)) {
      acc.set(networkName, []);
    }
    acc.get(networkName).push(service);
    return acc;
  }, new Map());

const measureGroup = (services, hasGateway) => {
  const teamKeys = [...new Set(services.map(getTeamKey))];
  const columns = Math.max(1, teamKeys.length || Math.min(services.length, 3));
  const rows = Math.max(
    1,
    ...teamKeys.map((teamKey) => services.filter((service) => getTeamKey(service) === teamKey).length),
  );
  const width = Math.max(620, GROUP_PADDING_X * 2 + columns * COLUMN_WIDTH);
  const serviceStart = hasGateway ? GROUP_HEADER + 86 : GROUP_HEADER;
  const height = Math.max(392, serviceStart + rows * ROW_HEIGHT + 68);

  return {
    columns,
    rows,
    width,
    height,
    serviceStart,
  };
};

const createMachineData = (service) => ({
  id: service.id,
  serviceName: service.serviceName,
  containerName: service.containerName,
  hostname: service.hostname,
  image: service.image,
  build: service.build,
  dockerfile: service.dockerfile,
  kind: service.kind,
  teamId: service.teamId,
  teamName: service.teamName,
  networks: service.networks,
  primaryNetwork: service.primaryNetwork,
  ipAddress: service.ipAddress,
  dependsOn: service.dependsOn,
  links: service.links,
  ports: service.ports,
  expose: service.expose,
  environment: service.environment,
  labels: service.labels,
  command: service.command,
  entrypoint: service.entrypoint,
  restart: service.restart,
  volumes: service.volumes,
  capAdd: service.capAdd,
  privileged: service.privileged,
  raw: service.raw,
});

const addEdge = (edges, seen, edge) => {
  const key = edgeKey(edge.source, edge.target, edge.data?.kind || edge.label || 'edge');
  if (seen.has(key) || edge.source === edge.target) {
    return;
  }

  seen.add(key);
  edges.push({
    id: key.replace(/[^a-zA-Z0-9:_>-]/g, '-'),
    type: 'smoothstep',
    markerEnd: {
      type: 'arrowclosed',
      width: 18,
      height: 18,
    },
    ...edge,
  });
};

const createNetworkGatewayNode = (network, groupId, position, color) => ({
  id: gatewayNodeId(network.name),
  type: 'machineNode',
  parentNode: groupId,
  extent: 'parent',
  position,
  draggable: true,
  data: {
    id: gatewayNodeId(network.name),
    serviceName: `${network.name} gateway`,
    teamName: 'Network',
    kind: 'gateway',
    ipAddress: network.gateway,
    subnet: network.subnet,
    primaryNetwork: network.name,
    networks: [
      {
        name: network.name,
        subnet: network.subnet,
        gateway: network.gateway,
      },
    ],
    ports: [],
    expose: [],
    environment: {},
    labels: {},
    dependsOn: [],
    links: [],
    volumes: [],
    capAdd: [],
    isSynthetic: true,
    accentColor: color,
  },
});

export const buildDockerFlow = (parsed) => {
  const nodes = [];
  const edges = [];
  const seenEdges = new Set();
  const nodeDetailsById = {};
  const servicesByNetwork = groupServicesByNetwork(parsed.services);
  const networks = buildNetworkCatalog(parsed);

  let cursorX = 0;
  let cursorY = 0;
  let currentRowHeight = 0;

  networks.forEach((network, index) => {
    const color = GROUP_PALETTE[index % GROUP_PALETTE.length];
    const services = [...(servicesByNetwork.get(network.name) || [])].sort(sortServices);
    const hasNetworkGateway = Boolean(network.gateway);
    const size = measureGroup(services, hasNetworkGateway);

    if (cursorX > 0 && cursorX + size.width > GROUP_MAX_ROW_WIDTH) {
      cursorX = 0;
      cursorY += currentRowHeight + GROUP_GAP;
      currentRowHeight = 0;
    }

    const groupId = networkNodeId(network.name);
    nodes.push({
      id: groupId,
      type: 'networkGroup',
      position: {
        x: cursorX,
        y: cursorY,
      },
      draggable: false,
      selectable: false,
      data: {
        name: network.name,
        driver: network.driver,
        subnet: network.subnet,
        gateway: network.gateway,
        serviceCount: services.length,
        color,
      },
      style: {
        width: size.width,
        height: size.height,
      },
    });

    if (hasNetworkGateway) {
      const gatewayNode = createNetworkGatewayNode(
        network,
        groupId,
        {
          x: GROUP_PADDING_X,
          y: GROUP_HEADER - 18,
        },
        color,
      );
      nodes.push(gatewayNode);
      nodeDetailsById[gatewayNode.id] = gatewayNode.data;
    }

    const teamKeys = [...new Set(services.map(getTeamKey))];
    const teamIndex = new Map(teamKeys.map((teamKey, column) => [teamKey, column]));
    const servicesByTeam = services.reduce((acc, service) => {
      const teamKey = getTeamKey(service);
      if (!acc.has(teamKey)) {
        acc.set(teamKey, []);
      }
      acc.get(teamKey).push(service);
      return acc;
    }, new Map());

    servicesByTeam.forEach((teamServices, teamKey) => {
      const column = teamIndex.get(teamKey) || 0;
      const sortedTeamServices = [...teamServices].sort(sortServices);

      sortedTeamServices.forEach((service, row) => {
        const x = GROUP_PADDING_X + column * COLUMN_WIDTH;
        const y = size.serviceStart + row * ROW_HEIGHT;

        const node = {
          id: service.id,
          type: 'machineNode',
          parentNode: groupId,
          extent: 'parent',
          position: {
            x,
            y,
          },
          data: {
            ...createMachineData(service),
            accentColor: color,
          },
          style: {
            width: MACHINE_WIDTH,
            height: MACHINE_HEIGHT,
          },
        };

        nodes.push(node);
        nodeDetailsById[node.id] = node.data;

        if (hasNetworkGateway) {
          addEdge(edges, seenEdges, {
            source: gatewayNodeId(network.name),
            target: service.id,
            label: network.name,
            data: {
              kind: 'network',
            },
            style: {
              stroke: color,
              strokeWidth: 1.6,
              strokeOpacity: 0.45,
              strokeDasharray: '7 7',
            },
          });
        }
      });
    });

    cursorX += size.width + GROUP_GAP;
    currentRowHeight = Math.max(currentRowHeight, size.height);
  });

  const serviceByName = new Map(parsed.services.map((service) => [service.serviceName, service]));

  parsed.services.forEach((service) => {
    service.dependsOn.forEach((dependency) => {
      if (!serviceByName.has(dependency)) {
        return;
      }

      addEdge(edges, seenEdges, {
        source: dependency,
        target: service.serviceName,
        label: 'depends_on',
        animated: true,
        data: {
          kind: 'depends_on',
        },
        style: {
          stroke: '#38bdf8',
          strokeWidth: 2.4,
        },
      });
    });

    service.links.forEach((link) => {
      if (!serviceByName.has(link)) {
        return;
      }

      addEdge(edges, seenEdges, {
        source: service.serviceName,
        target: link,
        label: 'link',
        data: {
          kind: 'link',
        },
        style: {
          stroke: '#a78bfa',
          strokeWidth: 2.2,
        },
      });
    });
  });

  const servicesByTeam = parsed.services.reduce((acc, service) => {
    if (!service.teamId) {
      return acc;
    }
    if (!acc.has(service.teamId)) {
      acc.set(service.teamId, []);
    }
    acc.get(service.teamId).push(service);
    return acc;
  }, new Map());

  servicesByTeam.forEach((teamServices) => {
    const gateways = teamServices.filter((service) => service.kind === 'gateway');
    const targets = teamServices.filter((service) => service.kind !== 'gateway');

    gateways.forEach((gateway) => {
      targets.forEach((target) => {
        addEdge(edges, seenEdges, {
          source: gateway.serviceName,
          target: target.serviceName,
          label: 'team access',
          data: {
            kind: 'team-access',
          },
          style: {
            stroke: '#fbbf24',
            strokeWidth: 2,
          },
        });
      });
    });
  });

  return {
    nodes,
    edges,
    nodeDetailsById,
  };
};
