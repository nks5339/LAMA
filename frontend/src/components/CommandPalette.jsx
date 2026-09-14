import { useState, useEffect, useRef, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { 
  Search, 
  BookOpen, 
  Database, 
  Boxes, 
  Code2, 
  Activity, 
  Terminal, 
  Library, 
  FileText,
  Settings,
  ChevronRight,
} from "lucide-react";

/**
 * CommandPalette - Professional quick navigation (Cmd/Ctrl+K)
 * 
 * Features:
 * - Fuzzy search across pages, actions, recent files
 * - Keyboard navigation (↑↓ + Enter)
 * - Grouped results
 * - Recent items
 */
export default function CommandPalette({ isOpen, onClose }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef(null);

  // Focus input when opened
  useEffect(() => {
    if (isOpen && inputRef.current) {
      inputRef.current.focus();
      setQuery("");
      setSelectedIndex(0);
    }
  }, [isOpen]);

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e) => {
      if (!isOpen) return;

      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => Math.min(i + 1, filteredItems.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const item = filteredItems[selectedIndex];
        if (item) {
          handleSelect(item);
        }
      }
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [isOpen, selectedIndex]);

  const allItems = useMemo(() => [
    // Navigation
    { type: "nav", label: "Discovery & SRS", icon: BookOpen, path: "/", keywords: ["upload", "chat", "srs", "requirements"] },
    { type: "nav", label: "Data Model", icon: Database, path: "/data-model", keywords: ["oltp", "olap", "ddl", "schema", "erd"] },
    { type: "nav", label: "Architecture", icon: Boxes, path: "/architecture", keywords: ["microservices", "hld", "lld", "api"] },
    { type: "nav", label: "Code Generation", icon: Code2, path: "/code-gen", keywords: ["codegen", "source", "github"] },
    { type: "nav", label: "Living System", icon: Activity, path: "/living", keywords: ["tests", "selenium", "monitoring"] },
    { type: "nav", label: "Integrations", icon: ChevronRight, path: "/integrations", keywords: ["third-party", "apis"] },
    
    // Tools
    { type: "tool", label: "Console", icon: Terminal, path: "/console", keywords: ["models", "agents", "usage"] },
    { type: "tool", label: "Ontology Studio", icon: Library, path: "/ontology-studio", keywords: ["owl", "kb", "knowledge"] },
    { type: "tool", label: "Prompt Library", icon: FileText, path: "/prompts", keywords: ["templates"] },
    { type: "tool", label: "Audit Log", icon: FileText, path: "/audit", keywords: ["history", "changes"] },
    { type: "tool", label: "Settings", icon: Settings, path: "/settings", keywords: ["github", "config"] },
  ], []);

  const filteredItems = useMemo(() => {
    if (!query.trim()) return allItems;
    
    const q = query.toLowerCase();
    return allItems.filter(item => 
      item.label.toLowerCase().includes(q) ||
      item.keywords.some(k => k.includes(q))
    );
  }, [query, allItems]);

  const handleSelect = (item) => {
    if (item.action) {
      item.action();
    } else if (item.path) {
      navigate(item.path);
    }
    onClose();
  };

  if (!isOpen) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/40 backdrop-blur-sm z-50 animate-in fade-in duration-150"
        onClick={onClose}
        data-testid="command-palette-backdrop"
      />

      {/* Palette */}
      <div
        className="fixed top-20 left-1/2 -translate-x-1/2 w-full max-w-2xl bg-white rounded-lg shadow-2xl border border-[#E6E6E6] z-50 animate-in fade-in slide-in-from-top-4 duration-200"
        data-testid="command-palette"
      >
        {/* Search Input */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-[#E6E6E6]">
          <Search className="w-5 h-5 text-[#9CA3AF] shrink-0" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Type to search pages, actions, files..."
            className="flex-1 text-[15px] text-[#2E2E38] placeholder:text-[#9CA3AF] outline-none bg-transparent"
            data-testid="command-palette-input"
          />
          <kbd className="hidden sm:inline-block px-2 py-0.5 bg-[#F6F6FA] border border-[#E6E6E6] rounded text-[11px] font-mono text-[#6B7280]">
            ESC
          </kbd>
        </div>

        {/* Results */}
        <div className="max-h-96 overflow-y-auto mos-scroll">
          {filteredItems.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-[#9CA3AF]">
              No results found for "{query}"
            </div>
          ) : (
            <div className="py-2">
              {/* Group by type */}
              {["nav", "tool"].map(type => {
                const items = filteredItems.filter(i => i.type === type);
                if (items.length === 0) return null;

                return (
                  <div key={type}>
                    <div className="px-4 py-1.5 text-[11px] font-semibold text-[#9CA3AF] uppercase tracking-wide">
                      {type === "nav" ? "Pages" : "Tools"}
                    </div>
                    {items.map((item, idx) => {
                      const globalIdx = filteredItems.indexOf(item);
                      const isSelected = globalIdx === selectedIndex;

                      return (
                        <button
                          key={globalIdx}
                          onClick={() => handleSelect(item)}
                          onMouseEnter={() => setSelectedIndex(globalIdx)}
                          className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                            isSelected 
                              ? "bg-[#FFFCE6] border-l-2 border-[#FFE600]" 
                              : "hover:bg-[#F6F6FA] border-l-2 border-transparent"
                          }`}
                          data-testid={`command-item-${item.label.toLowerCase().replace(/\s+/g, "-")}`}
                        >
                          <item.icon className={`w-4 h-4 shrink-0 ${isSelected ? "text-[#2E2E38]" : "text-[#9CA3AF]"}`} />
                          <span className={`flex-1 text-sm font-medium ${isSelected ? "text-[#2E2E38]" : "text-[#4B5563]"}`}>
                            {item.label}
                          </span>
                          {isSelected && (
                            <kbd className="px-1.5 py-0.5 bg-white border border-[#E6E6E6] rounded text-[10px] font-mono text-[#6B7280]">
                              ↵
                            </kbd>
                          )}
                        </button>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Footer Hints */}
        <div className="flex items-center justify-between px-4 py-2 border-t border-[#E6E6E6] bg-[#F6F6FA]">
          <div className="flex items-center gap-4 text-[11px] text-[#6B7280]">
            <div className="flex items-center gap-1">
              <kbd className="px-1.5 py-0.5 bg-white border border-[#E6E6E6] rounded font-mono">↑↓</kbd>
              <span>Navigate</span>
            </div>
            <div className="flex items-center gap-1">
              <kbd className="px-1.5 py-0.5 bg-white border border-[#E6E6E6] rounded font-mono">↵</kbd>
              <span>Select</span>
            </div>
            <div className="flex items-center gap-1">
              <kbd className="px-1.5 py-0.5 bg-white border border-[#E6E6E6] rounded font-mono">ESC</kbd>
              <span>Close</span>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
