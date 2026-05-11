import type { Edge, Node } from 'reactflow';
import type {
  MachineNodeData,
  ParsedCompose,
  ServiceDefinition,
  TopologyEdgeData,
  TopologyNodeData,
} from '../types';

const GROUP_PALETTE = [
  '#38bdf8',
  '#a78bfa',
  '#34d399',
  '#f87171',
  '#f472b6',
  '#fbbf24',
];

const MACHINE_WIDTH = 196;
const MACHINE_HEIGHT = 104;
const NETWORK_PADDING_X = 96;
const NETWORK_PADDING_BOTTOM = 96;
const NETWORK_HEADER = 118;
const PAIR_WIDTH = 240;
const PAIR_HEIGHT = 276;
const PAIR_COLUMN_GAP = 180;
const PAIR_ROW_GAP = 116;
const GROUP_GAP = 150;
const GROUP_MAX_ROW_WIDTH = 2600;
const MIN_NETWORK_WIDTH = 1160;

const networkNodeId = (name: string) => `network:${name}`;
const edgeKey = (source: string, target: string, kind: string) => `${source}->${target}:${kind}`;

const teamSortValue = (service: ServiceDefinition) => {
  const numeric = Number.parseInt(service.teamId || '', 10);
  if (!Number.isNaN(numeric)) {
    return numeric;
  }
  return Number.MAX_SAFE_INTEGER;
};

const sortServices = (a: ServiceDefinition, b: ServiceDefinition) => {
  const teamDelta = teamSortValue(a) - teamSortValue(b);
  if (teamDelta !== 0) {
    return teamDelta;
  }
  return a.serviceName.localeCompare(b.serviceName);
};

const getTeamKey = (service: ServiceDefinition) =>
  service.teamId ? `team-${service.teamId}` : `${service.teamName}:${service.serviceName}`;

const getServiceRole = (service: ServiceDefinition) => {
  const text = [service.serviceName, service.containerName, service.hostname, service.image]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();

  if (service.kind === 'gateway' || /(ssh|gateway|jump|bastion)/.test(text)) {
    return 'ssh';
  }
  if (/(vuln|challenge|app|api|web|http|service)/.test(text) || service.kind === 'service') {
    return 'vuln';
  }
  if (service.kind === 'database') {
    return 'database';
  }
  if (service.kind === 'firewall') {
    return 'firewall';
  }
  return 'other';
};

const getShortLabel = (role: string, service: ServiceDefinition) => {
  if (role === 'ssh') {
    return 'S';
  }
  if (role === 'vuln') {
    return 'V';
  }
  if (role === 'database') {
    return 'DB';
  }
  if (role === 'firewall') {
    return 'FW';
  }
  return service.serviceName.slice(0, 2).toUpperCase();
};

const buildNetworkCatalog = (parsed: ParsedCompose) => {
  const catalog = new Map(parsed.networks.map((network) => [network.name, network]));

  parsed.services.forEach((service) => {
    const networkName = service.primaryNetwork || 'external';
    if (!catalog.has(networkName)) {
      catalog.set(networkName, {
        name: networkName,
        driver: networkName === 'external' ? 'external' : 'default',
        external: networkName === 'external',
        raw: {},
      });
    }
  });

  if (catalog.size === 0) {
    catalog.set('external', {
      name: 'external',
      driver: 'external',
      external: true,
      raw: {},
    });
  }

  return [...catalog.values()];
};

const groupServicesByNetwork = (services: ServiceDefinition[]) =>
  services.reduce((acc, service) => {
    const networkName = service.primaryNetwork || 'external';
    if (!acc.has(networkName)) {
      acc.set(networkName, []);
    }
    acc.get(networkName)?.push(service);
    return acc;
  }, new Map<string, ServiceDefinition[]>());

const createMachineData = (service: ServiceDefinition): MachineNodeData => {
  const relationRole = getServiceRole(service);

  return {
    ...service,
    relationRole,
    shortLabel: getShortLabel(relationRole, service),
  };
};

type EdgeInput = Edge<TopologyEdgeData> & {
  markerEnd?: Edge<TopologyEdgeData>['markerEnd'] | null;
};

const addEdge = (edges: Array<Edge<TopologyEdgeData>>, seen: Set<string>, edge: EdgeInput) => {
  const key = edgeKey(edge.source, edge.target, edge.data?.kind || edge.label || 'edge');
  if (seen.has(key) || edge.source === edge.target) {
    return;
  }

  const markerEnd =
    edge.markerEnd === null
      ? undefined
      : edge.markerEnd || {
          type: 'arrowclosed',
          width: 16,
          height: 16,
        };

  seen.add(key);
  edges.push({
    id: key.replace(/[^a-zA-Z0-9:_>-]/g, '-'),
    type: 'smoothstep',
    ...edge,
    markerEnd,
  });
};

const getGrid = (itemCount: number) => {
  if (itemCount <= 0) {
    return {
      columns: 1,
      rows: 1,
    };
  }

  const columns = itemCount <= 3 ? itemCount : Math.ceil(Math.sqrt(itemCount * 1.45));

  return {
    columns,
    rows: Math.ceil(itemCount / columns),
  };
};

type TeamGroup = {
  teamKey: string;
  teamId?: string;
  teamName: string;
  sshServices: ServiceDefinition[];
  vulnServices: ServiceDefinition[];
  otherServices: ServiceDefinition[];
  services: ServiceDefinition[];
};

const buildTeamGroups = (services: ServiceDefinition[]): TeamGroup[] => {
  const grouped = services.reduce((acc, service) => {
    const teamKey = getTeamKey(service);
    if (!acc.has(teamKey)) {
      acc.set(teamKey, []);
    }
    acc.get(teamKey)?.push(service);
    return acc;
  }, new Map<string, ServiceDefinition[]>());

  return [...grouped.entries()]
    .map(([teamKey, teamServices]) => {
      const sorted = [...teamServices].sort(sortServices);
      const ssh = sorted.find((service) => getServiceRole(service) === 'ssh');
      const vuln = sorted.find((service) => getServiceRole(service) === 'vuln');
      const primary = ssh || vuln || sorted[0];

      return {
        teamKey,
        teamId: primary?.teamId,
        teamName: primary?.teamName || teamKey,
        sshServices: sorted.filter((service) => getServiceRole(service) === 'ssh'),
        vulnServices: sorted.filter((service) => getServiceRole(service) === 'vuln'),
        otherServices: sorted.filter((service) => !['ssh', 'vuln'].includes(getServiceRole(service))),
        services: sorted,
      };
    })
    .sort((a, b) => {
      const aValue = Number.parseInt(a.teamId || '', 10);
      const bValue = Number.parseInt(b.teamId || '', 10);
      if (!Number.isNaN(aValue) && !Number.isNaN(bValue) && aValue !== bValue) {
        return aValue - bValue;
      }
      return a.teamName.localeCompare(b.teamName);
    });
};

const measureNetwork = (teamGroups: TeamGroup[]) => {
  const grid = getGrid(teamGroups.length);
  const width = Math.max(
    MIN_NETWORK_WIDTH,
    NETWORK_PADDING_X * 2 + grid.columns * PAIR_WIDTH + (grid.columns - 1) * PAIR_COLUMN_GAP,
  );
  const height =
    NETWORK_HEADER +
    NETWORK_PADDING_BOTTOM +
    grid.rows * PAIR_HEIGHT +
    Math.max(0, grid.rows - 1) * PAIR_ROW_GAP;

  return {
    ...grid,
    width,
    height,
  };
};

const getTeamPosition = (index: number, layout: { columns: number; width: number; count: number }) => {
  const row = Math.floor(index / layout.columns);
  const column = index % layout.columns;
  const itemsInRow = Math.min(layout.columns, layout.count - row * layout.columns);
  const rowWidth = itemsInRow * PAIR_WIDTH + Math.max(0, itemsInRow - 1) * PAIR_COLUMN_GAP;
  const fullWidth = layout.width - NETWORK_PADDING_X * 2;
  const rowOffset = Math.max(0, (fullWidth - rowWidth) / 2);

  return {
    x: NETWORK_PADDING_X + rowOffset + column * (PAIR_WIDTH + PAIR_COLUMN_GAP),
    y: NETWORK_HEADER + row * (PAIR_HEIGHT + PAIR_ROW_GAP),
  };
};

const placeTeamServices = (teamGroup: TeamGroup, basePosition: { x: number; y: number }) => {
  const placements: Array<{ service: ServiceDefinition; x: number; y: number }> = [];
  const centerX = basePosition.x + (PAIR_WIDTH - MACHINE_WIDTH) / 2;
  const topY = basePosition.y;
  const bottomY = basePosition.y + MACHINE_HEIGHT + 64;

  teamGroup.sshServices.forEach((service, index) => {
    placements.push({
      service,
      x: centerX + index * 18,
      y: topY + index * 16,
    });
  });

  teamGroup.vulnServices.forEach((service, index) => {
    placements.push({
      service,
      x: centerX + index * 18,
      y: bottomY + index * 16,
    });
  });

  const sideBySide = teamGroup.sshServices.length === 0 || teamGroup.vulnServices.length === 0;
  teamGroup.otherServices.forEach((service, index) => {
    placements.push({
      service,
      x: sideBySide
        ? centerX
        : basePosition.x + PAIR_WIDTH + 22 + (index % 2) * (MACHINE_WIDTH + 20),
      y: sideBySide ? topY + index * (MACHINE_HEIGHT + 28) : topY + Math.floor(index / 2) * (MACHINE_HEIGHT + 28),
    });
  });

  return placements;
};

const getTeamId = (service: ServiceDefinition) => service.teamId || service.teamName || service.serviceName;

export const buildDockerFlow = (parsed: ParsedCompose) => {
  const nodes: Array<Node<TopologyNodeData>> = [];
  const edges: Array<Edge<TopologyEdgeData>> = [];
  const seenEdges = new Set<string>();
  const nodeDetailsById: Record<string, MachineNodeData> = {};
  const servicesByNetwork = groupServicesByNetwork(parsed.services);
  const networks = buildNetworkCatalog(parsed);

  let cursorX = 0;
  let cursorY = 0;
  let currentRowHeight = 0;

  networks.forEach((network, index) => {
    const color = GROUP_PALETTE[index % GROUP_PALETTE.length];
    const services = [...(servicesByNetwork.get(network.name) || [])].sort(sortServices);
    const teamGroups = buildTeamGroups(services);
    const size = measureNetwork(teamGroups);

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
        teamCount: teamGroups.length,
        color,
      },
      style: {
        width: size.width,
        height: size.height,
      },
    });

    teamGroups.forEach((teamGroup, teamIndex) => {
      const basePosition = getTeamPosition(teamIndex, {
        ...size,
        count: teamGroups.length,
      });
      const placements = placeTeamServices(teamGroup, basePosition);

      placements.forEach(({ service, x, y }) => {
        const data: MachineNodeData = {
          ...createMachineData(service),
          accentColor: getServiceRole(service) === 'ssh'
            ? '#fbbf24'
            : getServiceRole(service) === 'vuln'
              ? '#f87171'
              : color,
        };

        const node: Node<TopologyNodeData> = {
          id: service.id,
          type: 'machineNode',
          parentNode: groupId,
          extent: 'parent',
          position: {
            x,
            y,
          },
          data,
          style: {
            width: MACHINE_WIDTH,
            height: MACHINE_HEIGHT,
          },
        };

        nodes.push(node);
        nodeDetailsById[node.id] = data;
      });

      teamGroup.sshServices.forEach((ssh) => {
        teamGroup.vulnServices.forEach((vuln) => {
          addEdge(edges, seenEdges, {
            source: ssh.serviceName,
            target: vuln.serviceName,
            sourceHandle: 'bottom',
            targetHandle: 'top',
            markerEnd: null,
            data: {
              kind: 'team-pair',
              label: 'team service',
              defaultVisible: true,
            },
            style: {
              stroke: '#cbd5e1',
              strokeWidth: 1.8,
              strokeOpacity: 0.36,
            },
          });
        });
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
        hidden: true,
        data: {
          kind: 'depends_on',
          label: 'depends_on',
          revealOnHover: true,
        },
        style: {
          stroke: '#38bdf8',
          strokeWidth: 2,
          strokeOpacity: 0.52,
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
        hidden: true,
        data: {
          kind: 'link',
          label: 'link',
          revealOnHover: true,
        },
        style: {
          stroke: '#a78bfa',
          strokeWidth: 2,
          strokeOpacity: 0.52,
        },
      });
    });
  });

  const sshServices = parsed.services.filter((service) => getServiceRole(service) === 'ssh');
  const vulnServices = parsed.services.filter((service) => getServiceRole(service) === 'vuln');

  sshServices.forEach((ssh) => {
    vulnServices.forEach((vuln) => {
      if (getTeamId(ssh) === getTeamId(vuln)) {
        return;
      }

      const sharedNetwork = ssh.networks.some((sshNetwork) =>
        vuln.networks.some((vulnNetwork) => vulnNetwork.name === sshNetwork.name),
      );

      if (!sharedNetwork) {
        return;
      }

      addEdge(edges, seenEdges, {
        source: ssh.serviceName,
        target: vuln.serviceName,
        sourceHandle: 'right',
        targetHandle: 'left',
        hidden: true,
        data: {
          kind: 'attack',
          label: 'can attack',
          revealOnHover: true,
        },
        animated: true,
        style: {
          stroke: '#fb7185',
          strokeWidth: 2.2,
          strokeOpacity: 0.34,
          strokeDasharray: '8 8',
        },
      });
    });
  });

  return {
    nodes,
    edges,
    nodeDetailsById,
  };
};