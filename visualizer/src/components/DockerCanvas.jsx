import { useCallback, useEffect, useMemo } from 'react';
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

const CanvasInner = ({ topology, onSelectNode }) => {
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
  }, [setEdges, setNodes, topology]);

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
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={handleNodeClick}
      onPaneClick={handlePaneClick}
      fitView
      fitViewOptions={{ padding: 0.18, includeHiddenNodes: false }}
      minZoom={0.25}
      maxZoom={1.45}
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
  );
};

const DockerCanvas = (props) => (
  <ReactFlowProvider>
    <CanvasInner {...props} />
  </ReactFlowProvider>
);

export default DockerCanvas;
