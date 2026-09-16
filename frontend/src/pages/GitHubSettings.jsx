import { useEffect, useState } from "react";
import {
  Github,
  Folder,
  Lock,
  CheckCircle2,
  XCircle,
  Cloud,
  User,
  KeyRound,
} from "lucide-react";
import { useProjects } from "@/state/ProjectContext";
import HelpIcon from "@/components/HelpIcon";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import api from "@/lib/api";
import { toast } from "sonner";

const FOLDER_TREE = `pmis-modernized/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI route files per domain
│   │   ├── models/       # Pydantic models
│   │   ├── services/     # Business logic
│   │   └── db/           # Database layer
│   ├── migrations/       # Alembic PostgreSQL migrations
│   ├── tests/            # pytest unit tests
│   └── main.py
├── frontend/             # React app
├── schema/               # PostgreSQL DDL files
├── docs/                 # Generated SRS PDF / SRS.md
├── Dockerfile
├── docker-compose.yml
└── README.md`;

export default function GitHubSettingsPage() {
  const { active } = useProjects();
  const [form, setForm] = useState({
    repo_url: "",
    token: "",
    username: "",
    password: "",
    branch: "main",
  });
  // "token" | "basic"
  const [authMethod, setAuthMethod] = useState("token");
  const [saved, setSaved] = useState(false);
  const [hasToken, setHasToken] = useState(false);
  const [hasPassword, setHasPassword] = useState(false);
  const [savedUsername, setSavedUsername] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [saving, setSaving] = useState(false);
  const [pushing, setPushing] = useState(false);

  useEffect(() => {
    if (!active?.id) return;
    api.get(`/github/config/${active.id}`).then((r) => {
      setForm((f) => ({
        ...f,
        repo_url: r.data.repo_url || "",
        branch: r.data.branch || "main",
        username: r.data.username || "",
      }));
      setHasToken(!!r.data.has_token);
      setHasPassword(!!r.data.has_password);
      setSavedUsername(r.data.username || "");
      setSaved(!!r.data.repo_url);
      setAuthMethod(r.data.auth_method === "basic" ? "basic" : "token");
    });
  }, [active?.id]);

  if (!active) {
    return (
      <div className="flex-1 flex items-center justify-center text-fg-subtle">
        <div className="text-sm">No active project.</div>
      </div>
    );
  }

  const credentialsProvided = () => {
    if (authMethod === "token") return !!form.token || hasToken;
    return (!!form.username || !!savedUsername) && (!!form.password || hasPassword);
  };

  const buildCredentialPayload = () => {
    if (authMethod === "token") {
      return { token: form.token || "", username: "", password: "" };
    }
    return {
      token: "",
      username: form.username || savedUsername || "",
      password: form.password || "",
    };
  };

  const handleSave = async () => {
    if (!form.repo_url) {
      toast.error("Repo URL is required");
      return;
    }
    if (authMethod === "token") {
      if (!form.token && !hasToken) {
        toast.error("Personal access token is required");
        return;
      }
      // If switching from basic → token but no token typed, block.
      if (!form.token && !hasToken) {
        toast.error("Enter your token to switch to token auth.");
        return;
      }
    } else {
      if (!form.username && !savedUsername) {
        toast.error("Username is required");
        return;
      }
      if (!form.password && !hasPassword) {
        toast.error("Password is required");
        return;
      }
    }
    setSaving(true);
    try {
      const payload = {
        project_id: active.id,
        repo_url: form.repo_url,
        branch: form.branch,
      };
      if (authMethod === "token") {
        if (form.token) payload.token = form.token;
      } else {
        const u = form.username || savedUsername;
        if (u) payload.username = u;
        if (form.password) payload.password = form.password;
      }
      await api.post("/github/config", payload);
      toast.success("GitHub config saved");
      setSaved(true);
      if (authMethod === "token") {
        setHasToken(true);
        setHasPassword(false);
        setSavedUsername("");
      } else {
        setHasPassword(true);
        setHasToken(false);
        setSavedUsername(payload.username || savedUsername);
      }
      setForm((f) => ({ ...f, token: "", password: "" }));
    } catch (e) {
      toast.error("Save failed", { description: e.response?.data?.detail || e.message });
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    if (!form.repo_url) {
      toast.error("Repo URL is required for test");
      return;
    }
    if (!credentialsProvided()) {
      toast.error(
        authMethod === "token"
          ? "Token is required for test"
          : "Username and password are required for test",
      );
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const creds = buildCredentialPayload();
      const r = await api.post("/github/test", { repo_url: form.repo_url, ...creds });
      setTestResult(r.data);
      if (r.data.ok) toast.success(`Connected to ${r.data.repo_name}`);
      else toast.error("Connection failed", { description: r.data.error });
    } catch (e) {
      toast.error("Test failed", { description: e.message });
    } finally {
      setTesting(false);
    }
  };

  const handlePushSRS = async () => {
    setPushing(true);
    try {
      const creds = buildCredentialPayload();
      const r = await api.post("/github/push", {
        project_id: active.id,
        repo_url: form.repo_url,
        branch: form.branch,
        ...creds,
      });
      if (r.data.status === "success") {
        toast.success("SRS pushed", { description: r.data.message });
      } else {
        toast.error("Push failed", { description: r.data.message });
      }
    } catch (e) {
      toast.error("Push failed", { description: e.message });
    } finally {
      setPushing(false);
    }
  };

  const tabBtn = (id, label, Icon) => (
    <button
      type="button"
      data-testid={`gh-auth-tab-${id}`}
      onClick={() => setAuthMethod(id)}
      className={`text-xs px-3 py-1.5 border rounded-sm flex items-center gap-1 ${
        authMethod === id
          ? "bg-ink text-ink-fg border-fg"
          : "bg-surface text-fg-muted border-border hover:bg-surface-2"
      }`}
    >
      <Icon className="w-3 h-3" />
      {label}
    </button>
  );

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0">
      <header className="bg-surface border-b border-border px-6 py-3">
        <div className="text-micro uppercase tracking-widest text-fg-subtle">Settings</div>
        <h1 className="font-display text-lg font-bold tracking-tight text-fg flex items-center">
          <Github className="w-5 h-5 mr-2" />
          GitHub Configuration
          <HelpIcon
            text="Configure GitHub repository and credentials (personal access token OR username + password) for pushing generated code."
            testId="help-github-page"
          />
        </h1>
      </header>

      <div className="flex-1 overflow-y-auto mos-scroll p-6 bg-bg">
        <div className="max-w-3xl mx-auto space-y-6">
          {/* Section A — GitHub Config */}
          <section className="mos-panel p-6" data-testid="section-github-config">
            <h2 className="font-display text-sm font-bold tracking-tight text-fg mb-1">
              Repository Connection
            </h2>
            <p className="text-xs text-fg-subtle mb-4">
              Saved per project. Credentials are kept on the server. Choose either a personal access
              token or a username + password.
            </p>

            <div className="space-y-4">
              <div>
                <Label htmlFor="gh-repo" className="flex items-center text-xs font-semibold uppercase tracking-wider text-fg-muted">
                  GitHub Repository
                  <HelpIcon text="The target GitHub repository where LAMA will push generated code, schema files, and Dockerfile. Must exist before pushing." testId="help-gh-repo" />
                </Label>
                <Input
                  id="gh-repo"
                  data-testid="gh-repo"
                  placeholder="https://github.com/org/pmis-modernized"
                  value={form.repo_url}
                  onChange={(e) => setForm({ ...form, repo_url: e.target.value })}
                  className="mt-1 rounded-sm"
                />
              </div>

              {/* Auth method switch */}
              <div>
                <div className="flex items-center text-xs font-semibold uppercase tracking-wider text-fg-muted mb-1">
                  Authentication Method
                  <HelpIcon
                    text="Use a personal access token (recommended) OR a GitHub username + password."
                    testId="help-gh-auth-method"
                  />
                </div>
                <div className="flex gap-2" data-testid="gh-auth-tabs">
                  {tabBtn("token", "Personal Access Token", KeyRound)}
                  {tabBtn("basic", "Username + Password", User)}
                </div>
              </div>

              {authMethod === "token" && (
                <div data-testid="gh-token-block">
                  <Label htmlFor="gh-token" className="flex items-center text-xs font-semibold uppercase tracking-wider text-fg-muted">
                    <Lock className="w-3 h-3 mr-1" />
                    Personal Access Token
                    <HelpIcon text="GitHub PAT with repo write permission. Never stored in logs. Kept on the server only." testId="help-gh-token" />
                  </Label>
                  <Input
                    id="gh-token"
                    data-testid="gh-token"
                    type="password"
                    placeholder={hasToken ? "Token saved — enter again to replace" : "ghp_… or github_pat_…"}
                    value={form.token}
                    onChange={(e) => setForm({ ...form, token: e.target.value })}
                    className="mt-1 rounded-sm font-mono text-xs"
                  />
                </div>
              )}

              {authMethod === "basic" && (
                <div className="space-y-3" data-testid="gh-basic-block">
                  <div>
                    <Label htmlFor="gh-username" className="flex items-center text-xs font-semibold uppercase tracking-wider text-fg-muted">
                      <User className="w-3 h-3 mr-1" />
                      Username
                      <HelpIcon text="Your GitHub username (e.g. octocat)." testId="help-gh-username" />
                    </Label>
                    <Input
                      id="gh-username"
                      data-testid="gh-username"
                      placeholder={savedUsername ? savedUsername : "github-username"}
                      value={form.username}
                      onChange={(e) => setForm({ ...form, username: e.target.value })}
                      className="mt-1 rounded-sm"
                    />
                  </div>
                  <div>
                    <Label htmlFor="gh-password" className="flex items-center text-xs font-semibold uppercase tracking-wider text-fg-muted">
                      <Lock className="w-3 h-3 mr-1" />
                      Password
                      <HelpIcon
                        text="GitHub account password. Note: github.com requires a personal access token for HTTPS Git operations; basic auth still works for the REST API and self-hosted GitHub Enterprise."
                        testId="help-gh-password"
                      />
                    </Label>
                    <Input
                      id="gh-password"
                      data-testid="gh-password"
                      type="password"
                      placeholder={hasPassword ? "Password saved — enter again to replace" : "••••••••"}
                      value={form.password}
                      onChange={(e) => setForm({ ...form, password: e.target.value })}
                      className="mt-1 rounded-sm font-mono text-xs"
                    />
                  </div>
                </div>
              )}

              <div>
                <Label htmlFor="gh-branch" className="flex items-center text-xs font-semibold uppercase tracking-wider text-fg-muted">
                  Target Branch
                  <HelpIcon text="Default branch where LAMA commits. Usually 'main'." testId="help-gh-branch" />
                </Label>
                <Input
                  id="gh-branch"
                  data-testid="gh-branch"
                  value={form.branch}
                  onChange={(e) => setForm({ ...form, branch: e.target.value })}
                  className="mt-1 rounded-sm"
                />
              </div>

              <div className="flex items-center gap-2 pt-2">
                <Button
                  data-testid="gh-save-btn"
                  onClick={handleSave}
                  disabled={saving}
                  className="bg-ink text-ink-fg hover:bg-ink-hover rounded-sm"
                >
                  {saving ? "Saving…" : "Save GitHub Config"}
                </Button>
                <Button
                  data-testid="gh-test-btn"
                  onClick={handleTest}
                  disabled={testing}
                  variant="outline"
                  className="bg-surface border-border hover:bg-surface-2 text-fg-muted rounded-sm"
                >
                  <Cloud className="w-4 h-4 mr-1" />
                  {testing ? "Testing…" : "Test Connection"}
                </Button>
                {testResult && (
                  <div
                    className={`flex items-center gap-1 text-xs ${testResult.ok ? "text-emerald-700" : "text-red-700"}`}
                    data-testid="gh-test-result"
                  >
                    {testResult.ok ? <CheckCircle2 className="w-4 h-4" /> : <XCircle className="w-4 h-4" />}
                    {testResult.ok ? `${testResult.repo_name} (${testResult.default_branch})` : testResult.error}
                  </div>
                )}
              </div>
            </div>
          </section>

          {/* Section B — Folder Tree Preview */}
          {saved && (
            <section className="mos-panel p-6" data-testid="section-folder-tree">
              <div className="flex items-start justify-between mb-3">
                <div>
                  <h2 className="font-display text-sm font-bold tracking-tight text-fg flex items-center">
                    <Folder className="w-4 h-4 mr-1.5" />
                    Target Folder Structure
                    <HelpIcon text="LAMA will push this structure to GitHub in Stage 4 (Code Generation). Schema files are pushed after Stage 2 (Data Model)." testId="help-folder-tree" />
                  </h2>
                  <p className="text-xs text-fg-subtle mt-1">Read-only preview of what will be committed at each stage.</p>
                </div>
                <Button
                  data-testid="push-srs-btn"
                  onClick={handlePushSRS}
                  disabled={pushing}
                  className="bg-ink text-ink-fg hover:bg-ink-hover rounded-sm text-xs h-8"
                >
                  {pushing ? "Pushing…" : "Push SRS to GitHub"}
                </Button>
              </div>
              <pre
                data-testid="folder-tree"
                className="bg-surface-2 border border-border rounded-sm p-4 text-[12px] font-mono text-fg overflow-x-auto leading-relaxed"
              >
{FOLDER_TREE}
              </pre>
              <div className="mt-3 text-micro text-fg-subtle space-y-1">
                <div>· <b>Stage 1 (SRS freeze):</b> pushes <code>docs/SRS.md</code></div>
                <div>· <b>Stage 2 (DataModel freeze):</b> pushes <code>schema/*.sql</code></div>
                <div>· <b>Stage 4 (CodeGen):</b> pushes full <code>backend/</code> + <code>frontend/</code> + Dockerfile</div>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
