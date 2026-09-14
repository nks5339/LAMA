import { useState, useEffect } from "react";
import { Wifi, WifiOff, Database, Server, Cpu, Activity } from "lucide-react";
import { useProjects } from "@/state/ProjectContext";

/**
 * StatusBar - Professional IDE-style bottom status bar
 * 
 * Shows:
 * - Backend connection status
 * - Current project status
 * - Model usage stats (from MiniConsole)
 * - System health indicators
 */
export default function StatusBar() {
  const { active } = useProjects();
  const [backendStatus, setBackendStatus] = useState("checking");
  const [tokenStats, setTokenStats] = useState(null);

  // Poll backend health
  useEffect(() => {
    const check = async () => {
      try {
        const res = await fetch("/health");
        setBackendStatus(res.ok ? "online" : "offline");
      } catch {
        setBackendStatus("offline");
      }
    };
    check();
    const interval = setInterval(check, 30000); // 30s
    return () => clearInterval(interval);
  }, []);

  // Listen to token stats from MiniConsole (if available)
  useEffect(() => {
    const handler = (e) => {
      if (e.detail?.tokens) {
        setTokenStats(e.detail.tokens);
      }
    };
    window.addEventListener("lama:token-stats", handler);
    return () => window.removeEventListener("lama:token-stats", handler);
  }, []);

  return (
    <div 
      className="h-6 shrink-0 flex items-center justify-between px-3 bg-[#2E2E38] text-white text-[11px] font-medium border-t border-[#1A1A24] z-10"
      data-testid="status-bar"
    >
      {/* Left: Backend Status */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          {backendStatus === "online" ? (
            <>
              <Wifi className="w-3 h-3 text-[#10B981]" />
              <span className="text-[#10B981]">Connected</span>
            </>
          ) : backendStatus === "offline" ? (
            <>
              <WifiOff className="w-3 h-3 text-[#EF4444]" />
              <span className="text-[#EF4444]">Offline</span>
            </>
          ) : (
            <>
              <Activity className="w-3 h-3 text-[#FFE600] animate-pulse" />
              <span className="text-[#9CA3AF]">Checking...</span>
            </>
          )}
        </div>

        {active && (
          <>
            <div className="w-px h-3 bg-[#4B5563]" />
            <div className="flex items-center gap-1.5">
              <Database className="w-3 h-3 text-[#9CA3AF]" />
              <span className="text-[#D1D5DB]">{active.name}</span>
            </div>
          </>
        )}
      </div>

      {/* Right: Token Stats & System Info */}
      <div className="flex items-center gap-4">
        {tokenStats && (
          <>
            <div className="flex items-center gap-1.5">
              <Cpu className="w-3 h-3 text-[#9CA3AF]" />
              <span className="text-[#D1D5DB]">
                {formatNumber(tokenStats.input + tokenStats.output)} tokens
              </span>
            </div>
            <div className="w-px h-3 bg-[#4B5563]" />
          </>
        )}
        
        <div className="flex items-center gap-1.5">
          <Server className="w-3 h-3 text-[#9CA3AF]" />
          <span className="text-[#9CA3AF]">LAMA v1.0</span>
        </div>
      </div>
    </div>
  );
}

function formatNumber(num) {
  if (num >= 1000000) return (num / 1000000).toFixed(1) + "M";
  if (num >= 1000) return (num / 1000).toFixed(1) + "K";
  return num.toString();
}
