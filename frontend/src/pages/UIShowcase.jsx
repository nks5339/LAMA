import { useState } from "react";
import { 
  Layout, 
  Table, 
  FolderTree, 
  Settings, 
  Terminal,
  Play,
  Save,
  Download,
  RefreshCw,
  Boxes
} from "lucide-react";
import { 
  DataTable, 
  TreeView, 
  PropertyInspector, 
  Toolbar 
} from "@/components/ux";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

/**
 * UI Showcase Page
 * 
 * Demonstrates all professional UI/UX components:
 * - DataTable (sortable, searchable, exportable)
 * - TreeView (file explorer style)
 * - PropertyInspector (properties panel)
 * - Toolbar (action bar)
 * - Professional layout patterns
 */
export default function UIShowcase() {
  const [selectedNode, setSelectedNode] = useState(null);
  const [selectedRow, setSelectedRow] = useState(null);
  const [activeTab, setActiveTab] = useState("components");

  // Sample data for DataTable
  const sampleData = [
    { id: 1, name: "User Service", type: "Microservice", status: "Running", port: 8001, version: "1.2.3" },
    { id: 2, name: "Auth Service", type: "Microservice", status: "Running", port: 8002, version: "2.1.0" },
    { id: 3, name: "Payment Gateway", type: "API", status: "Stopped", port: 8003, version: "1.0.5" },
    { id: 4, name: "Notification Service", type: "Worker", status: "Running", port: 8004, version: "3.0.1" },
    { id: 5, name: "Analytics Engine", type: "Batch", status: "Running", port: 8005, version: "1.5.2" },
  ];

  const tableColumns = [
    { key: "name", label: "Service Name", width: "30%" },
    { key: "type", label: "Type", width: "20%" },
    { 
      key: "status", 
      label: "Status", 
      width: "15%",
      render: (value) => (
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
          value === "Running" 
            ? "bg-green-100 text-green-800" 
            : "bg-red-100 text-red-800"
        }`}>
          {value}
        </span>
      )
    },
    { key: "port", label: "Port", width: "15%" },
    { key: "version", label: "Version", width: "20%" },
  ];

  // Sample data for TreeView
  const treeData = [
    {
      key: "src",
      label: "src",
      children: [
        {
          key: "src/components",
          label: "components",
          children: [
            { key: "src/components/Header.jsx", label: "Header.jsx" },
            { key: "src/components/Sidebar.jsx", label: "Sidebar.jsx" },
            { key: "src/components/Footer.jsx", label: "Footer.jsx" },
          ]
        },
        {
          key: "src/pages",
          label: "pages",
          children: [
            { key: "src/pages/Home.jsx", label: "Home.jsx" },
            { key: "src/pages/Dashboard.jsx", label: "Dashboard.jsx" },
          ]
        },
        { key: "src/App.jsx", label: "App.jsx" },
        { key: "src/index.js", label: "index.js" },
      ]
    },
    {
      key: "public",
      label: "public",
      children: [
        { key: "public/index.html", label: "index.html" },
        { key: "public/favicon.ico", label: "favicon.ico" },
      ]
    },
    { key: "package.json", label: "package.json" },
    { key: "README.md", label: "README.md" },
  ];

  // Sample properties for PropertyInspector
  const getProperties = () => {
    if (selectedRow) {
      return [
        { section: "Basic Info", label: "ID", value: selectedRow.id },
        { section: "Basic Info", label: "Name", value: selectedRow.name, editable: true, onChange: () => {} },
        { section: "Basic Info", label: "Type", value: selectedRow.type },
        { section: "Configuration", label: "Port", value: selectedRow.port, type: "number", editable: true },
        { section: "Configuration", label: "Version", value: selectedRow.version },
        { section: "Configuration", label: "Auto-restart", value: true, type: "checkbox", editable: true },
        { section: "Advanced", label: "Health Check URL", value: `/health/${selectedRow.port}`, editable: true },
        { section: "Advanced", label: "Environment", value: "production", type: "select", editable: true, options: [
          { label: "Development", value: "development" },
          { label: "Staging", value: "staging" },
          { label: "Production", value: "production" },
        ]},
      ];
    }
    
    if (selectedNode) {
      return [
        { section: "File Info", label: "Path", value: selectedNode.key },
        { section: "File Info", label: "Name", value: selectedNode.label },
        { section: "File Info", label: "Type", value: selectedNode.children ? "Directory" : "File" },
      ];
    }

    return [];
  };

  // Toolbar actions
  const toolbarActions = [
    {
      type: "group",
      items: [
        { label: "Run", icon: Play, onClick: () => alert("Run"), variant: "primary" },
        { label: "Save", icon: Save, onClick: () => alert("Save") },
      ]
    },
    { type: "divider" },
    { label: "Refresh", icon: RefreshCw, onClick: () => alert("Refresh") },
    { label: "Export", icon: Download, onClick: () => alert("Export") },
  ];

  const secondaryActions = [
    { label: "Settings", icon: Settings, onClick: () => alert("Settings") },
    { label: "Terminal", icon: Terminal, onClick: () => alert("Terminal") },
    { type: "divider" },
    { label: "Clear All", onClick: () => alert("Clear") },
  ];

  return (
    <div className="flex-1 flex flex-col bg-bg overflow-hidden">
      {/* Page Header */}
      <div className="px-6 py-4 bg-surface border-b border-border">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-fg font-display">
              Professional UI Components
            </h1>
            <p className="text-sm text-fg-muted mt-1">
              IDE-like interface components for LAMA
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs font-mono px-2 py-1 bg-brand-tint text-fg rounded border border-brand">
              v14.0
            </span>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <Tabs value={activeTab} onValueChange={setActiveTab} className="flex-1 flex flex-col">
          <div className="px-6 pt-3 bg-surface border-b border-border">
            <TabsList className="bg-transparent border-0 h-auto p-0 gap-6">
              <TabsTrigger 
                value="components" 
                className="bg-transparent border-b-2 border-transparent data-[state=active]:border-brand rounded-none px-0 pb-2"
              >
                <Layout className="w-4 h-4 mr-2" />
                Components
              </TabsTrigger>
              <TabsTrigger 
                value="layout" 
                className="bg-transparent border-b-2 border-transparent data-[state=active]:border-brand rounded-none px-0 pb-2"
              >
                <Boxes className="w-4 h-4 mr-2" />
                Layout Patterns
              </TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="components" className="flex-1 overflow-hidden mt-0">
            <div className="h-full flex">
              {/* Left: Component Demos */}
              <div className="flex-1 overflow-auto mos-scroll p-6">
                <div className="max-w-6xl space-y-6">
                  {/* Toolbar Demo */}
                  <section className="pro-card overflow-hidden">
                    <div className="px-4 py-3 bg-surface-2 border-b border-border">
                      <h3 className="text-sm font-semibold text-fg">Toolbar Component</h3>
                      <p className="text-xs text-fg-muted mt-0.5">Professional action bar with grouped buttons</p>
                    </div>
                    <Toolbar actions={toolbarActions} secondaryActions={secondaryActions} />
                  </section>

                  {/* DataTable Demo */}
                  <section className="pro-card overflow-hidden">
                    <div className="px-4 py-3 bg-surface-2 border-b border-border">
                      <h3 className="text-sm font-semibold text-fg flex items-center gap-2">
                        <Table className="w-4 h-4" />
                        DataTable Component
                      </h3>
                      <p className="text-xs text-fg-muted mt-0.5">
                        Sortable, searchable table with selection
                      </p>
                    </div>
                    <div className="h-80">
                      <DataTable
                        data={sampleData}
                        columns={tableColumns}
                        onRowClick={(row) => {
                          setSelectedRow(row);
                          setSelectedNode(null);
                        }}
                        searchable
                        sortable
                        exportable
                        onExport={() => alert("Export clicked")}
                      />
                    </div>
                  </section>

                  {/* TreeView Demo */}
                  <section className="pro-card overflow-hidden">
                    <div className="px-4 py-3 bg-surface-2 border-b border-border">
                      <h3 className="text-sm font-semibold text-fg flex items-center gap-2">
                        <FolderTree className="w-4 h-4" />
                        TreeView Component
                      </h3>
                      <p className="text-xs text-fg-muted mt-0.5">
                        File explorer style navigation tree
                      </p>
                    </div>
                    <div className="p-3">
                      <TreeView
                        data={treeData}
                        onNodeClick={(node) => {
                          setSelectedNode(node);
                          setSelectedRow(null);
                        }}
                        selectedKey={selectedNode?.key}
                      />
                    </div>
                  </section>
                </div>
              </div>

              {/* Right: PropertyInspector */}
              <PropertyInspector
                title={selectedRow ? `Service: ${selectedRow.name}` : selectedNode ? selectedNode.label : "Properties"}
                properties={getProperties()}
                className="w-80 shrink-0"
              />
            </div>
          </TabsContent>

          <TabsContent value="layout" className="flex-1 overflow-auto mos-scroll p-6 mt-0">
            <div className="max-w-4xl mx-auto space-y-6">
              <div className="pro-card p-6">
                <h3 className="text-lg font-semibold text-fg mb-4">Professional Layout Patterns</h3>
                
                <div className="space-y-4 text-sm text-fg-muted">
                  <div>
                    <h4 className="font-semibold text-fg mb-2">IDE-Style Layout</h4>
                    <ul className="list-disc list-inside space-y-1 ml-2">
                      <li>Top Toolbar: Breadcrumbs, search (⌘K), user profile</li>
                      <li>Left Sidebar: Hierarchical navigation with icons</li>
                      <li>Main Canvas: Multi-tab workspace with resizable panels</li>
                      <li>Right Panel: Context-sensitive property inspector</li>
                      <li>Bottom Status Bar: Connection status, token usage</li>
                    </ul>
                  </div>

                  <div>
                    <h4 className="font-semibold text-fg mb-2">Design System</h4>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <p className="font-medium mb-1">Typography:</p>
                        <ul className="text-xs space-y-0.5 ml-2">
                          <li>Title: 20px Chivo Bold</li>
                          <li>Heading: 16px Chivo Semibold</li>
                          <li>Body: 14px IBM Plex Sans</li>
                          <li>Caption: 12px IBM Plex Sans</li>
                        </ul>
                      </div>
                      <div>
                        <p className="font-medium mb-1">Spacing (8px base):</p>
                        <ul className="text-xs space-y-0.5 ml-2">
                          <li>xs: 4px, sm: 8px, md: 12px</li>
                          <li>lg: 16px, xl: 24px, 2xl: 32px</li>
                        </ul>
                      </div>
                    </div>
                  </div>

                  <div>
                    <h4 className="font-semibold text-fg mb-2">Color Palette</h4>
                    <div className="flex gap-2 flex-wrap">
                      <div className="flex items-center gap-2">
                        <div className="w-8 h-8 bg-brand border border-border rounded" />
                        <span className="text-xs">Primary Yellow</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <div className="w-8 h-8 bg-ink border border-border rounded" />
                        <span className="text-xs">Dark Text</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <div className="w-8 h-8 bg-bg border border-border rounded" />
                        <span className="text-xs">Background</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
