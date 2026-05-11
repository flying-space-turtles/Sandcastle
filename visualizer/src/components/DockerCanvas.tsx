import { useCallback, useEffect, useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  type DefaultEdgeOptions,
  type Edge,
  type Node,
  type NodeTypes,
} from 'reactflow';
import CustomMachineNode from './CustomMachineNode';
import CustomNetworkGroup from './CustomNetworkGroup';
import type { LiveEvent, MachineNodeData, Topology, TopologyEdgeData, TopologyNodeData } from '../types';

const LIVE_EDGE_COLORS: Record<string, string> = {
  sqli: '#ef4444',
  cmdi: '#f97316',
  'path-traversal': '#a855f7',
  ssh: '#fbbf24',
  icmp: '#22c55e',
  http: '#38bdf8',
  tcp: '#64748b',
};

const defaultEdgeOptions: DefaultEdgeOptions = {
  labelBgPadding: [8, 4],
  labelBgBorderRadius: 4,
  labelBgStyle: {
    fill: '#0f172a',
    fillOpacity: 0.86,
  },
  labelStyle: {
    fill: '#dbeafe',
    fontSize: 11,
    fontWeight: 600,
  },
};

const shouldRevealEdge = (edge: Edge<TopologyEdgeData>, hoveredId?: string | null) => {
  if (!hoveredId) {
    return Boolean(edge.data?.defaultVisible);
  }

  if (edge.data?.kind === 'attack') {
    return edge.source === hoveredId || edge.target === hoveredId;
  }

  if (edge.data?.revealOnHover) {
    return edge.source === hoveredId || edge.target === hoveredId;
  }

  return Boolean(edge.data?.defaultVisible);
};

const shouldHighlightEdge = (edge: Edge<TopologyEdgeData>, hoveredId?: string | null) =>
  Boolean(hoveredId) && (edge.source === hoveredId || edge.target === hoveredId);

type LiveEdgeInput = {
  id: string;
  source: string;
  target: string;
  event: LiveEvent;
  label: string;
  sourceHandle?: string;
  targetHandle?: string;
};

const makeLiveEdge = ({
  id,
  source,
  target,
  event,
  label,
  sourceHandle,
  targetHandle,
}: LiveEdgeInput): Edge<TopologyEdgeData> => ({
  id,
  source,
  target,
  sourceHandle,
  targetHandle,
  type: 'smoothstep',
  animated: true,
  selectable: false,
  focusable: false,
  data: { kind: 'live', eventType: event.type },
  style: {
    stroke: LIVE_EDGE_COLORS[event.type] || '#64748b',
    strokeWidth: 3,
    strokeOpacity: 0.92,
  },
  markerEnd: {
    type: 'arrowclosed',
    width: 14,
    height: 14,
    color: LIVE_EDGE_COLORS[event.type] || '#64748b',
  },
  label,
});

const getHoverSummary = (node: Node<MachineNodeData> | null, edges: Array<Edge<TopologyEdgeData>>) => {
  if (!node) {
    return null;
  }

  const attackEdges = edges.filter(
    (edge) => edge.data?.kind === 'attack' && (edge.source === node.id || edge.target === node.id),
  );
  const localEdges = edges.filter(
    (edge) => edge.data?.kind === 'team-pair' && (edge.source === node.id || edge.target === node.id),
  );
  const dependencyEdges = edges.filter(
    (edge) => ['depends_on', 'link'].includes(edge.data?.kind || '') &&
      (edge.source === node.id || edge.target === node.id),
  );

  if (node.data?.relationRole === 'ssh') {
    return `${node.data.serviceName} can reach ${attackEdges.length} opposing vulnerable app${attackEdges.length === 1 ? '' : 's'}.`;
  }

  if (node.data?.relationRole === 'vuln') {
    return `${node.data.serviceName} is reachable from ${attackEdges.length} opposing SSH container${attackEdges.length === 1 ? '' : 's'}.`;
  }

  if (node.data?.relationRole === 'firewall') {
    return `${node.data.serviceName} routes and masks team-to-team traffic.`;
  }

  const relationCount = dependencyEdges.length + localEdges.length;
  return `${node.data.serviceName} has ${relationCount} highlighted relation${relationCount === 1 ? '' : 's'}.`;
};

type DockerCanvasProps = {
  topology: Topology;
  onSelectNode: (node: MachineNodeData | null) => void;
  liveEdges: LiveEvent[];
};

const getNodeColor = (node: Node<TopologyNodeData>) => {
  if (node.data && 'accentColor' in node.data && node.data.accentColor) {
    return node.data.accentColor;
  }
  if (node.data && 'color' in node.data && node.data.color) {
    return node.data.color;
  }
  return '#38bdf8';
};

const CanvasInner = ({ topology, onSelectNode, liveEdges }: DockerCanvasProps) => {
  const [hoveredNode, setHoveredNode] = useState<Node<MachineNodeData> | null>(null);
  const nodeTypes = useMemo<NodeTypes>(
    () => ({
      machineNode: CustomMachineNode,
      networkGroup: CustomNetworkGroup,
    }),
    [],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState(topology.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(topology.edges);
  const [liveFlowEdges, setLiveFlowEdges] = useState<Array<Edge<TopologyEdgeData>>>([]);

  useEffect(() => {
    const firewallNodeId = topology.firewallNodeId;
    setLiveFlowEdges(
      (liveEdges || []).flatMap((event) => {
        if (!firewallNodeId || event.src === firewallNodeId || event.dst === firewallNodeId) {
          return [
            makeLiveEdge({
              id: `live:${event.src}||${event.dst}`,
              source: event.src,
              target: event.dst,
              event,
              label: (event.type || 'tcp').toUpperCase(),
            }),
          ];
        }

        return [
          makeLiveEdge({
            id: `live:${event.src}||${firewallNodeId}`,
            source: event.src,
            target: firewallNodeId,
            event,
            label: (event.type || 'tcp').toUpperCase(),
            sourceHandle: 'right',
            targetHandle: 'left',
          }),
          makeLiveEdge({
            id: `live:${firewallNodeId}||${event.dst}`,
            source: firewallNodeId,
            target: event.dst,
            event,
            label: event.maskedSrcIp ? `MASK ${event.maskedSrcIp}` : 'MASKED',
            sourceHandle: 'right',
            targetHandle: 'left',
          }),
        ];
      })
    );
  }, [liveEdges, topology.firewallNodeId]);

  useEffect(() => {
    setNodes(topology.nodes);
    setEdges(topology.edges);
    setHoveredNode(null);
  }, [setEdges, setNodes, topology]);

  const applyHoverState = useCallback(
    (hovered: Node<MachineNodeData> | null) => {
      const hoveredId = hovered?.id;
      const relatedNodeIds = new Set(hoveredId ? [hoveredId] : []);

      topology.edges.forEach((edge) => {
        if (hoveredId && shouldRevealEdge(edge, hoveredId) && shouldHighlightEdge(edge, hoveredId)) {
          relatedNodeIds.add(edge.source);
          relatedNodeIds.add(edge.target);
        }
      });

      setNodes((currentNodes) =>
        currentNodes.map((node) => {
          if (node.type !== 'machineNode') {
            return node;
          }

          const isHovered = node.id === hoveredId;
          const isRelated = Boolean(hoveredId) && relatedNodeIds.has(node.id) && !isHovered;

          return {
            ...node,
            data: {
              ...node.data,
              isHovered,
              isRelated,
              isDimmed: Boolean(hoveredId) && !isHovered && !isRelated,
            },
          };
        }),
      );

      setEdges((currentEdges) =>
        currentEdges.map((edge) => {
          const isVisible = shouldRevealEdge(edge, hoveredId);
          const isHighlighted = shouldHighlightEdge(edge, hoveredId);
          const isAttack = edge.data?.kind === 'attack';
          const isDefault = edge.data?.defaultVisible;

          return {
            ...edge,
            label: isHighlighted && edge.data?.label ? edge.data.label : undefined,
            hidden: !isVisible,
            animated: isAttack && isHighlighted,
            style: {
              ...edge.style,
              strokeOpacity: isHighlighted ? (isAttack ? 0.78 : 0.86) : isDefault ? 0.24 : 0.2,
              strokeWidth: isHighlighted ? (isAttack ? 2.8 : 2.5) : edge.style?.strokeWidth || 1.8,
            },
          };
        }),
      );

      setHoveredNode(hovered);
    },
    [setEdges, setNodes, topology.edges],
  );

  const handleNodeClick = useCallback(
    (_event: ReactMouseEvent, node: Node<TopologyNodeData>) => {
      if (node.type !== 'machineNode') {
        return;
      }
      onSelectNode(node.data as MachineNodeData);
    },
    [onSelectNode],
  );

  const handlePaneClick = useCallback(() => {
    onSelectNode(null);
  }, [onSelectNode]);

  const displayEdges = useMemo(() => [...edges, ...liveFlowEdges], [edges, liveFlowEdges]);

  return (
    <>
      <ReactFlow
        nodes={nodes}
        edges={displayEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        onNodeMouseEnter={(_event, node) => {
          if (node.type === 'machineNode') {
            applyHoverState(node as Node<MachineNodeData>);
          }
        }}
        onNodeMouseLeave={() => applyHoverState(null)}
        onPaneClick={handlePaneClick}
        fitView
        fitViewOptions={{ padding: 0.18, includeHiddenNodes: false }}
        minZoom={0.2}
        maxZoom={1.55}
        defaultEdgeOptions={defaultEdgeOptions}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#334155" gap={24} size={1.2} variant="dots" />
        <Controls position="bottom-left" showInteractive={false} />
        <MiniMap
          position="bottom-right"
          pannable
          zoomable
          nodeBorderRadius={6}
          nodeColor={getNodeColor}
          maskColor="rgba(2, 6, 23, 0.74)"
        />
      </ReactFlow>
      {hoveredNode && <div className="canvas-relation-hint">{getHoverSummary(hoveredNode, topology.edges)}</div>}
    </>
  );
};

const DockerCanvas = ({ topology, onSelectNode, liveEdges }: DockerCanvasProps) => (
  <ReactFlowProvider>
    <CanvasInner topology={topology} onSelectNode={onSelectNode} liveEdges={liveEdges} />
  </ReactFlowProvider>
);

export default DockerCanvas;
