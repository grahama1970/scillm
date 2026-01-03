import React from "react";

/**
 * Skeleton layout: left rail (sessions/settings), center chat, right inspector (collapsible).
 * Replace with real state/hooks when wiring data.
 */
export function ShellLayout() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 grid grid-cols-[280px_minmax(0,1fr)_360px] gap-4 p-4">
      <aside className="bg-white border border-slate-200 rounded-lg p-4">
        <div className="font-semibold mb-2">Sessions</div>
        <div className="text-sm text-slate-500">Session list goes here.</div>
        <div className="mt-4 font-semibold mb-2">Settings</div>
        <div className="text-sm text-slate-500">Model, temp, strict/repair toggles.</div>
      </aside>
      <main className="bg-white border border-slate-200 rounded-lg p-4 flex flex-col">
        <div className="flex items-center justify-between mb-3 text-sm text-slate-600">
          <div>Action bar: model select, temp, max_tokens, strict/repair, tool/mode</div>
          <button className="px-3 py-1 rounded-md bg-slate-900 text-white text-sm">New chat</button>
        </div>
        <div className="flex-1 space-y-3 overflow-auto">
          <div className="p-3 rounded-lg bg-slate-100 text-sm text-slate-700">User message…</div>
          <div className="p-3 rounded-lg bg-white border text-sm text-slate-800 shadow-sm">
            Assistant message… <span className="ml-2 text-xs text-slate-500">model • latency • repaired?</span>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-2">
          <textarea className="flex-1 border rounded-md p-2 text-sm" rows={2} placeholder="Message..." />
          <button className="px-4 py-2 rounded-md bg-blue-600 text-white text-sm">Send</button>
        </div>
      </main>
      <aside className="bg-white border border-slate-200 rounded-lg p-4">
        <div className="font-semibold mb-2">Inspector</div>
        <div className="text-sm text-slate-500">Raw / Pretty / Headers.</div>
        <div className="mt-4 font-semibold mb-2">Health</div>
        <div className="text-sm text-slate-500">redis / arangodb / ollama / codex-agent / chutes</div>
      </aside>
    </div>
  );
}
