interface SettingsPanelProps {
  liveMode: boolean
  onToggleLiveMode: (enabled: boolean) => void
  onRefresh: () => void
}

export default function SettingsPanel({
  liveMode,
  onToggleLiveMode,
  onRefresh,
}: SettingsPanelProps) {
  return (
    <div className="w-72 bg-slate-800 border-l border-slate-700 p-4 flex flex-col gap-4 overflow-y-auto">
      <h2 className="text-lg font-semibold text-cyan-400">Settings</h2>

      <div className="bg-slate-700 rounded-lg p-4">
        <div className="flex items-center justify-between">
          <label className="text-sm font-medium">Live Mode</label>
          <button
            onClick={() => onToggleLiveMode(!liveMode)}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition ${
              liveMode ? 'bg-green-600' : 'bg-slate-600'
            }`}
          >
            <span
              className={`inline-block h-4 w-4 transform rounded-full bg-white transition ${
                liveMode ? 'translate-x-6' : 'translate-x-1'
              }`}
            />
          </button>
        </div>
        <p className="text-xs text-slate-400 mt-2">
          {liveMode
            ? 'Fetching live container states from Docker'
            : 'Using static topology from config files'}
        </p>
      </div>

      <button
        onClick={onRefresh}
        className="w-full px-4 py-2 bg-cyan-600 hover:bg-cyan-500 rounded font-semibold text-sm transition"
      >
        Refresh
      </button>

      <div className="text-xs text-slate-400 space-y-1">
        <p>💡 Tip: Click nodes to view details</p>
        <p>📊 Drag to pan, scroll to zoom</p>
      </div>
    </div>
  )
}
