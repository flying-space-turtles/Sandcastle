import { Node } from 'reactflow'

interface NodeDetailProps {
  node: Node
  onClose: () => void
}

export default function NodeDetail({ node, onClose }: NodeDetailProps) {
  const data = node.data as Record<string, unknown>

  return (
    <div className="absolute bottom-4 right-80 w-80 bg-slate-800 border border-slate-600 rounded-lg shadow-xl z-10 max-h-96 overflow-y-auto">
      <div className="bg-slate-700 px-4 py-3 flex justify-between items-center border-b border-slate-600">
        <h3 className="font-semibold text-cyan-400">{node.id}</h3>
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-slate-200 text-lg"
        >
          ✕
        </button>
      </div>
      <div className="p-4 space-y-3">
        {Object.entries(data).map(([key, value]) => (
          <div key={key}>
            <p className="text-xs font-semibold text-slate-400 uppercase">{key}</p>
            <p className="text-sm text-slate-200 break-words font-mono">
              {typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
            </p>
          </div>
        ))}
      </div>
    </div>
  )
}
