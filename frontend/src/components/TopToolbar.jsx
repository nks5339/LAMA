import React from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ChevronRight, Search, Bell, Settings } from "lucide-react";
import { useProjects } from "@/state/ProjectContext";
import { useAuth } from "@/state/AuthContext";

/**
 * TopToolbar - Professional IDE-style top menu bar
 * 
 * Features:
 * - Breadcrumb navigation
 * - Global search (Cmd+K trigger)
 * - User profile
 * - Notifications
 * - Quick settings
 */
export default function TopToolbar({ onSearchClick }) {
  const location = useLocation();
  const navigate = useNavigate();
  const { active } = useProjects();
  const { user } = useAuth();

  const breadcrumbs = getBreadcrumbs(location.pathname, active);

  return (
    <div 
      className="h-12 shrink-0 flex items-center justify-between px-4 bg-white border-b border-[#E6E6E6] z-20"
      data-testid="top-toolbar"
    >
      {/* Left: Breadcrumbs */}
      <div className="flex items-center gap-2 min-w-0 flex-1">
        {breadcrumbs.map((crumb, idx) => (
          <React.Fragment key={idx}>
            {idx > 0 && (
              <ChevronRight className="w-3.5 h-3.5 text-[#9CA3AF] shrink-0" />
            )}
            {crumb.clickable ? (
              <button
                onClick={() => navigate(crumb.path)}
                className="text-sm text-[#6B7280] hover:text-[#2E2E38] font-medium truncate transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE600] rounded px-1"
              >
                {crumb.label}
              </button>
            ) : (
              <span className="text-sm text-[#2E2E38] font-semibold truncate">
                {crumb.label}
              </span>
            )}
          </React.Fragment>
        ))}
      </div>

      {/* Right: Actions */}
      <div className="flex items-center gap-1 shrink-0">
        {/* Global Search */}
        <button
          onClick={onSearchClick}
          className="h-8 px-3 flex items-center gap-2 rounded hover:bg-[#F6F6FA] text-[#6B7280] hover:text-[#2E2E38] transition-colors group"
          title="Search (⌘K)"
          data-testid="global-search-btn"
        >
          <Search className="w-4 h-4" />
          <div className="hidden md:flex items-center gap-1 text-xs">
            <kbd className="px-1.5 py-0.5 bg-[#F6F6FA] group-hover:bg-[#E6E6E6] border border-[#E6E6E6] rounded text-[10px] font-mono">
              ⌘K
            </kbd>
          </div>
        </button>

        {/* Notifications */}
        <button
          className="w-8 h-8 flex items-center justify-center rounded hover:bg-[#F6F6FA] text-[#6B7280] hover:text-[#2E2E38] transition-colors relative"
          title="Notifications"
          data-testid="notifications-btn"
        >
          <Bell className="w-4 h-4" />
          {/* Notification dot (placeholder) */}
          {/* <span className="absolute top-1.5 right-1.5 w-2 h-2 bg-[#EF4444] rounded-full border border-white" /> */}
        </button>

        {/* Settings */}
        <button
          onClick={() => navigate("/settings")}
          className="w-8 h-8 flex items-center justify-center rounded hover:bg-[#F6F6FA] text-[#6B7280] hover:text-[#2E2E38] transition-colors"
          title="Settings"
          data-testid="settings-btn"
        >
          <Settings className="w-4 h-4" />
        </button>

        {/* Divider */}
        <div className="w-px h-5 bg-[#E6E6E6] mx-1" />

        {/* User Profile */}
        <button
          className="h-8 px-2 flex items-center gap-2 rounded hover:bg-[#F6F6FA] transition-colors group"
          title={user?.email || "User"}
          data-testid="user-profile-btn"
        >
          <div className="w-6 h-6 bg-[#FFE600] text-[#2E2E38] rounded-full flex items-center justify-center text-xs font-bold">
            {getUserInitials(user)}
          </div>
          <span className="hidden lg:inline text-sm text-[#2E2E38] font-medium">
            {user?.username || user?.email?.split("@")[0] || "User"}
          </span>
        </button>
      </div>
    </div>
  );
}

/**
 * Generate breadcrumb trail from current path
 */
function getBreadcrumbs(pathname, activeProject) {
  const crumbs = [
    { label: activeProject?.name || "LAMA", path: "/", clickable: pathname !== "/" }
  ];

  const routes = {
    "/": null,
    "/data-model": { label: "Data Model", path: "/data-model" },
    "/architecture": { label: "Architecture", path: "/architecture" },
    "/code-gen": { label: "Code Generation", path: "/code-gen" },
    "/living": { label: "Living System", path: "/living" },
    "/integrations": { label: "Integrations", path: "/integrations" },
    "/console": { label: "Console", path: "/console" },
    "/ontology-studio": { label: "Ontology Studio", path: "/ontology-studio" },
    "/prompts": { label: "Prompt Library", path: "/prompts" },
    "/audit": { label: "Audit Log", path: "/audit" },
    "/settings": { label: "Settings", path: "/settings" },
  };

  const route = routes[pathname];
  if (route) {
    crumbs.push({ ...route, clickable: false });
  }

  return crumbs;
}

/**
 * Get user initials for avatar
 */
function getUserInitials(user) {
  if (!user) return "U";
  if (user.username) {
    const parts = user.username.split(" ");
    if (parts.length >= 2) return parts[0][0] + parts[1][0];
    return user.username.slice(0, 2).toUpperCase();
  }
  if (user.email) return user.email[0].toUpperCase();
  return "U";
}
