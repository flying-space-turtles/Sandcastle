import { useCallback, useEffect, useMemo, useState } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
} from 'reactflow';
import CustomMachineNode from './CustomMachineNode.jsx';
import CustomNetworkGroup from './CustomNetworkGroup.jsx';

const defaultEdgeOptions = {
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

const shouldRevealEdge = (edge, hoveredId) => {
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

const shouldHighlightEdge = (edge, hoveredId) =>
  Boolean(hoveredId) && (edge.source === hoveredId || edge.target === hoveredId);

const getHoverSummary = (node, edges) => {
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
    (edge) => ['depends_on', 'link'].includes(edge.data?.kind) && (edge.source === node.id || edge.target === node.id),
  );

  if (node.data?.relationRole === 'ssh') {
    return `${node.data.serviceName} can reach ${attackEdges.length} opposing vulnerable app${attackEdges.length === 1 ? '' : 's'}.`;
  }

  if (node.data?.relationRole === 'vuln') {
    return `${node.data.serviceName} is reachable from ${attackEdges.length} opposing SSH container${attackEdges.length === 1 ? '' : 's'}.`;
  }

  const relationCount = dependencyEdges.length + localEdges.length;
  return `${node.data.serviceName} has ${relationCount} highlighted relation${relationCount === 1 ? '' : 's'}.`;
};

const CanvasInner = ({ topology, onSelectNode }) => {
  const [hoveredNode, setHoveredNode] = useState(null);
  const nodeTypes = useMemo(
    () => ({
      machineNode: CustomMachineNode,
      networkGroup: CustomNetworkGroup,
    }),
    [],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState(topology.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(topology.edges);

  useEffect(() => {
    setNodes(topology.nodes);
    setEdges(topology.edges);
    setHoveredNode(null);
  }, [setEdges, setNodes, topology]);

  const applyHoverState = useCallback(
    (hovered) => {
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
    (_event, node) => {
      if (node.type !== 'machineNode') {
        return;
      }
      onSelectNode(node.data, node.id);
    },
    [onSelectNode],
  );

  const handlePaneClick = useCallback(() => {
    onSelectNode(null);
  }, [onSelectNode]);

  return (
    <>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        onNodeMouseEnter={(_event, node) => {
          if (node.type === 'machineNode') {
            applyHoverState(node);
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
          nodeColor={(node) => node.data?.accentColor || node.data?.color || '#38bdf8'}
          maskColor="rgba(2, 6, 23, 0.74)"
        />
      </ReactFlow>
      {hoveredNode && <div className="canvas-relation-hint">{getHoverSummary(hoveredNode, topology.edges)}</div>}
    </>
  );
};

const DockerCanvas = (props) => (
  <ReactFlowProvider>
    <CanvasInner {...props} />
  </ReactFlowProvider>
);

export default DockerCanvas;
