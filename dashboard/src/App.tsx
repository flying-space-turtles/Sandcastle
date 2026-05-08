import { useState, useEffect, useCallback } from 'react'
import ReactFlow, {
  Node,
  Edge,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
} from 'reactflow'
import 'reactflow/dist/style.css'
import axios from 'axios'
import SettingsPanel from './components/SettingsPanel'
import NodeDetail from './components/NodeDetail'

interface Topology {
  nodes: Node[]
  edges: Edge[]
  teams: Array<{ id: string; name: string }>
}

export default function App() {
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [topology, setTopology] = useState<Topology | null>(null)
  const [selectedNode, setSelectedNode] = useState<Node | null>(null)
  const [liveMode, setLiveMode] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchTopology()
  }, [liveMode])

  const fetchTopology = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const response = await axios.get('/api/topology', {
        params: { live: liveMode }
      })
      setTopology(response.data)
      setNodes(response.data.nodes)
      setEdges(response.data.edges)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load topology')
    } finally {
      setLoading(false)
    }
  }, [liveMode, setNodes, setEdges])

  const handleNodeClick = useCallback((event: React.MouseEvent, node: Node) => {
    setSelectedNode(node)
  }, [])

  const toggleLiveMode = useCallback(async (enabled: boolean) => {
    try {
      await axios.post('/api/settings', { live_mode: enabled })
      setLiveMode(enabled)
    } catch (err) {
      setError('Failed to update settings')
    }
  }, [])

  return (
    <div className="w-full h-screen flex bg-slate-900">
      <div className="flex-1 flex flex-col">
        <div className="bg-slate-800 border-b border-slate-700 px-4 py-3 shadow-lg">
          <h1 className="text-xl font-bold text-cyan-400">Sandcastle Topology</h1>
          <p className="text-xs text-slate-400 mt-1">
            {liveMode ? '🔴 Live Mode' : '⚪ Static Mode'} • {topology?.teams.length || 0} teams
          </p>
        </div>
        <div className="flex-1 relative">
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-black bg-opacity-50 z-10">
              <div className="text-center">
                <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-cyan-400 mx-auto"></div>
                <p className="mt-4 text-slate-300">Loading topology...</p>
              </div>
            </div>
          )}
          {error && (
            <div className="absolute inset-0 flex items-center justify-center bg-black bg-opacity-50 z-10">
              <div className="bg-red-900 border border-red-700 rounded-lg p-4 max-w-md">
                <p className="text-red-200">{error}</p>
                <button
                  onClick={() => fetchTopology()}
                  className="mt-3 px-4 py-2 bg-red-700 hover:bg-red-600 rounded text-sm font-semibold"
                >
                  Retry
                </button>
              </div>
            </div>
          )}
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeClick={handleNodeClick}
            fitView
          >
            <Background color="#334155" gap={16} size={1} />
            <Controls />
          </ReactFlow>
        </div>
      </div>

      {selectedNode && (
        <NodeDetail node={selectedNode} onClose={() => setSelectedNode(null)} />
      )}

      <SettingsPanel
        liveMode={liveMode}
        onToggleLiveMode={toggleLiveMode}
        onRefresh={fetchTopology}
      />
    </div>
  )
}
