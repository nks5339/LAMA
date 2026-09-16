import { useState, useEffect, useRef, useCallback } from "react";
import { X, MessageSquare, Send, Bot, User, Plus, Clock, Trash2, Pencil, Square, FileText } from "lucide-react";
import { streamMessage, createSession, getSession, listSessions, archiveSession, updateSRSSection } from "@/lib/api";
import { useProjects } from "@/state/ProjectContext";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ThinkingDots, AgentTimeline } from "@/components/ui/job-progress";
import { useElapsed, formatDuration, phaseLabel } from "@/hooks/useJobProgress";

const SECTION_OPTIONS = [
  { value: "purpose", label: "1. Purpose" },
  { value: "scope", label: "2. Scope" },
  { value: "definitions", label: "3. Definitions" },
  { value: "overall_description", label: "4. Overall Description" },
  { value: "functional_requirements", label: "5. Functional Requirements" },
  { value: "non_functional_requirements", label: "6. Non-Functional Requirements" },
  { value: "use_cases", label: "7. Use Cases" },
  { value: "constraints", label: "8. Constraints" },
  { value: "entity_relationship", label: "9. Entity Relationship" },
  { value: "system_features", label: "10. System Features" },
  { value: "business_requirements", label: "11. Business Requirements" },
  { value: "appendix", label: "12. Appendix" },
];

/**
 * FloatingChat - ChatGPT-like overlay chat interface
 * Triggered by a floating action button, shows chat history and sessions
 * 
 * @param {string} stage - The stage name (e.g., "Discovery", "DataModel")
 * @param {string} agentKey - The agent key (e.g., "srs.chat", "datamodel.chat")
 * @param {boolean} enableSrsEdit - Whether to show SRS edit mode toggle
 * @param {string} chatTitle - Title to show in the header
 * @param {Array} categories - Array of category objects {key, label} for chat tabs (optional)
 */
export default function FloatingChat({ 
  projectId, 
  kbReady, 
  model, 
  onConversationUpdated,
  stage = "Discovery",
  agentKey = "srs.chat",
  enableSrsEdit = true,
  chatTitle = "Discovery Chat",
  categories = null
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [activeSessionId, setActiveSessionId] = useState(null);
  const [, setActiveSession] = useState(null);
  const [history, setHistory] = useState([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [conversationId, setConversationId] = useState(null);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [showSidebar, setShowSidebar] = useState(true);
  const [srsEditMode, setSrsEditMode] = useState(false);
  const [editSection, setEditSection] = useState("functional_requirements");
  const [activeCategory, setActiveCategory] = useState(categories?.[0]?.key || null);
  const scrollRef = useRef(null);
  const { getSessionId, setSessionId } = useProjects();

  // Drives the "n s elapsed" line while a reply is in flight.
  const thinkingFor = useElapsed(sending);
  // Streaming state: tokens as they land, the current phase, and the
  // citations the answer is grounded in. `abortRef` backs the Stop button.
  const [draft, setDraft] = useState("");
  const [phase, setPhase] = useState(null);
  const [citations, setCitations] = useState([]);
  const abortRef = useRef(null);
  // The send handler reads citations after the stream closes, from inside an
  // async closure that captured the render-time value — so mirror them into
  // a ref rather than reading stale state.
  const citationsRef = useRef([]);

  const SESSION_STAGE = stage;
  const SESSION_AGENT = categories ? `${agentKey}.${activeCategory}` : agentKey;

  // Initialize with active session from context
  useEffect(() => {
    const savedSessionId = getSessionId(SESSION_STAGE, SESSION_AGENT);
    if (savedSessionId) {
      setActiveSessionId(savedSessionId);
    } else {
      // Reset when category changes and no saved session
      setActiveSessionId(null);
      setHistory([]);
    }
  }, [getSessionId, SESSION_AGENT, SESSION_STAGE]);
  
  // Reload sessions when category changes
  useEffect(() => {
    if (isOpen && projectId && categories) {
      loadSessions();
    }
  }, [activeCategory]);

  // Load sessions when opening
  const loadSessions = useCallback(async () => {
    if (!projectId) return;
    setLoadingSessions(true);
    try {
      const response = await listSessions(projectId, { 
        stage: SESSION_STAGE, 
        agentKey: SESSION_AGENT 
      });
      // Backend returns { sessions: [...], count: N }
      const sessionsList = Array.isArray(response?.sessions) ? response.sessions : [];
      setSessions(sessionsList);
      
      // Auto-select first session if none selected (but don't create new)
      if (sessionsList.length > 0 && !activeSessionId) {
        const firstSession = sessionsList[0];
        setActiveSessionId(firstSession.id);
        setSessionId(SESSION_STAGE, SESSION_AGENT, firstSession.id);
      }
    } catch (e) {
      console.error("Failed to load sessions:", e);
      setSessions([]);
    } finally {
      setLoadingSessions(false);
    }
  }, [projectId, activeSessionId, setSessionId, SESSION_STAGE, SESSION_AGENT]);

  useEffect(() => {
    if (isOpen && projectId) {
      loadSessions();
    }
  }, [isOpen, projectId, loadSessions]);

  // Load session details and history when active session changes
  useEffect(() => {
    if (!activeSessionId) {
      setActiveSession(null);
      setHistory([]);
      return;
    }
    
    (async () => {
      try {
        // Get session details which contain live_turns
        const sess = await getSession(activeSessionId);
        setActiveSession(sess);
        
        // Convert session's live_turns to chat history format
        if (sess?.live_turns && Array.isArray(sess.live_turns)) {
          const messages = [];
          sess.live_turns.forEach(turn => {
            if (turn.user_content) {
              messages.push({
                role: "user",
                content: turn.user_content,
                timestamp: turn.timestamp || sess.updated_at
              });
            }
            if (turn.assistant_content) {
              messages.push({
                role: "assistant",
                content: turn.assistant_content,
                timestamp: turn.timestamp || sess.updated_at
              });
            }
          });
          setHistory(messages);
        } else {
          setHistory([]);
        }
      } catch (e) {
        console.error("Failed to load session:", e);
        setHistory([]);
      }
    })();
  }, [activeSessionId]);

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [history, draft]);

  const handleNewSession = async () => {
    // Don't create session immediately - wait for first message
    // Just reset the UI to start fresh
    setActiveSessionId(null);
    setSessionId(SESSION_STAGE, SESSION_AGENT, null);
    setHistory([]);
    toast.success("New chat started - send a message to begin");
  };

  const handleArchiveSession = async (sessionId) => {
    try {
      await archiveSession(sessionId);
      toast.success("Session archived");
      loadSessions();
      if (activeSessionId === sessionId) {
        setActiveSessionId(null);
        setHistory([]);
      }
    } catch (e) {
      toast.error("Failed to archive session");
    }
  };

  const handleSend = async () => {
    if (!input.trim() || sending || !projectId) return;

    const userMsg = input.trim();
    setInput("");
    setSending(true);
    setDraft("");
    setCitations([]);
    citationsRef.current = [];
    setPhase("retrieving");

    // Optimistic user bubble.
    const tempMsg = { role: "user", content: userMsg, timestamp: new Date().toISOString() };
    setHistory((prev) => [...prev, tempMsg]);

    const controller = new AbortController();
    abortRef.current = controller;

    let streamed = "";
    try {
      // Create the session on the first message rather than up front, so an
      // abandoned "New chat" leaves no empty session behind.
      let sessionIdToUse = activeSessionId;
      if (!activeSessionId) {
        const sess = await createSession({
          project_id: projectId,
          stage: SESSION_STAGE,
          agent_key: SESSION_AGENT,
        });
        sessionIdToUse = sess.id;
        setActiveSessionId(sessionIdToUse);
        setSessionId(SESSION_STAGE, SESSION_AGENT, sessionIdToUse);
      }

      const payload = {
        project_id: projectId,
        message: userMsg,
        conversation_id: conversationId,
        model: model || undefined,
        session_id: sessionIdToUse || undefined,
        stage: SESSION_STAGE,
      };
      if (enableSrsEdit) {
        payload.edit_mode = srsEditMode;
        payload.selected_section = srsEditMode ? editSection : null;
      }

      const final = await streamMessage(
        payload,
        (evt) => {
          if (evt.type === "phase") setPhase(evt.phase);
          else if (evt.type === "citation") {
            setCitations((c) => {
              const next = c.some((x) => x.filename === evt.filename) ? c : [...c, evt];
              citationsRef.current = next;
              return next;
            });
          } else if (evt.type === "token") {
            streamed += evt.text;
            setDraft(streamed);
          }
        },
        controller.signal,
      );

      // Commit the streamed reply into history as one message.
      const assistantMsg = {
        role: "assistant",
        content: final?.message?.content || streamed,
        timestamp: final?.message?.created_at || new Date().toISOString(),
        _citations: citationsRef.current.length ? citationsRef.current : undefined,
      };
      if (enableSrsEdit && srsEditMode) {
        assistantMsg._editSection = editSection;
      }
      setHistory((prev) => [...prev, assistantMsg]);
      setDraft("");
      setConversationId(final?.conversation_id || conversationId);

      if (onConversationUpdated) {
        onConversationUpdated(final?.conversation_id, final?.srs_triggered || false);
      }
      // The backend used to swallow auto-trigger failures entirely. It now
      // reports them, so say so rather than leaving the user waiting for an
      // SRS that was never generated.
      if (final?.srs_error) {
        toast.error("SRS generation could not start", { description: final.srs_error });
      }
      if (!activeSessionId && sessionIdToUse) loadSessions();
    } catch (e) {
      if (e?.name === "AbortError") {
        // Stopped on purpose — keep whatever arrived so the user does not
        // lose a long partial answer.
        if (streamed.trim()) {
          setHistory((prev) => [...prev, {
            role: "assistant",
            content: streamed,
            timestamp: new Date().toISOString(),
            _stopped: true,
          }]);
        }
        setDraft("");
        toast.message("Stopped");
      } else {
        toast.error("Message failed", {
          description: e?.response?.data?.detail || e.message,
        });
        setHistory((prev) => prev.filter((m) => m !== tempMsg));
        setDraft("");
      }
    } finally {
      abortRef.current = null;
      setPhase(null);
      setSending(false);
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
  };


  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const applySrsEdit = async (content, section) => {
    try {
      await updateSRSSection(projectId, section, content);
      toast.success(`Applied to "${SECTION_OPTIONS.find((s) => s.value === section)?.label || section}"`);
      if (onConversationUpdated) {
        onConversationUpdated(conversationId, true);
      }
    } catch (e) {
      toast.error("Apply failed", { description: e.response?.data?.detail || e.message });
    }
  };

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="fixed bottom-6 right-6 w-14 h-14 bg-ink hover:bg-brand text-ink-fg hover:text-fg rounded-full shadow-lg flex items-center justify-center transition-all hover:scale-110 z-50"
        aria-label="Open Discovery Chat"
        data-testid="floating-chat-trigger"
      >
        <MessageSquare className="w-6 h-6" />
      </button>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 backdrop-blur-sm">
      <div className="bg-surface rounded-lg shadow-2xl w-[95vw] h-[90vh] max-w-7xl flex flex-col overflow-hidden">
        {/* Header */}
        <div className="bg-ink text-ink-fg px-6 py-3 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-3">
            <MessageSquare className="w-5 h-5" />
            <div>
              <h2 className="font-display text-lg font-bold">{chatTitle}</h2>
              <p className="text-xs text-fg-onDarkMuted">
                {activeSessionId 
                  ? `Session ${activeSessionId.slice(0, 8)} • ${history.length} messages`
                  : "AI-powered knowledge exploration"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {/* SRS Edit Mode Toggle - Only for Discovery */}
            {enableSrsEdit && (
              <button
                onClick={() => setSrsEditMode(!srsEditMode)}
                className={cn(
                  "text-xs px-3 py-1.5 rounded flex items-center gap-1.5 transition-colors",
                  srsEditMode
                    ? "bg-brand text-fg font-semibold"
                    : "bg-surface/10 text-white hover:bg-surface/20"
                )}
              >
                <Pencil className="w-3 h-3" />
                SRS Edit
              </button>
            )}
            
            <button
              onClick={() => setShowSidebar(!showSidebar)}
              className="hover:bg-surface/10 rounded px-3 py-1.5 transition-colors text-xs"
              title={showSidebar ? "Hide sessions" : "Show sessions"}
            >
              {showSidebar ? "Hide Sessions" : "Show Sessions"}
            </button>
            <button
              onClick={() => setIsOpen(false)}
              className="hover:bg-surface/10 rounded p-2 transition-colors"
              aria-label="Close chat"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* SRS Edit Mode strip - Only for Discovery */}
        {enableSrsEdit && srsEditMode && (
          <div className="px-6 py-2 bg-brand/10 border-b border-brand/40 flex items-center gap-3">
            <span className="text-micro uppercase tracking-wider text-fg font-semibold">Editing section:</span>
            <select
              value={editSection}
              onChange={(e) => setEditSection(e.target.value)}
              className="text-xs border border-border rounded px-2 py-1 bg-surface text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {SECTION_OPTIONS.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
            <span className="text-micro text-fg-muted">Describe what to add or change in this section.</span>
          </div>
        )}
        
        {/* Category tabs - For DataModel chat */}
        {categories && categories.length > 0 && (
          <div className="border-b border-border flex bg-surface">
            {categories.map((cat) => (
              <button
                key={cat.key}
                type="button"
                onClick={() => setActiveCategory(cat.key)}
                className={`text-xs px-4 py-2 font-semibold tracking-tight border-r border-border transition-colors ${
                  activeCategory === cat.key
                    ? "bg-ink text-ink-fg"
                    : "bg-surface text-fg-muted hover:text-fg hover:bg-surface-2"
                }`}
              >
                {cat.label}
              </button>
            ))}
          </div>
        )}

        {/* Main Content */}
        <div className="flex-1 flex overflow-hidden">
          {/* Sidebar - Chat Sessions */}
          {showSidebar && (
            <div className="w-64 bg-surface-2 border-r border-border flex flex-col shrink-0">
              <div className="p-4 border-b border-border">
                <button
                  onClick={handleNewSession}
                  className="w-full bg-brand hover:bg-brand-hover text-fg font-semibold py-2 px-4 rounded-lg flex items-center justify-center gap-2 transition-colors"
                >
                  <Plus className="w-4 h-4" />
                  New Chat
                </button>
              </div>

              <div className="flex-1 overflow-y-auto p-2">
                <h3 className="text-xs font-semibold text-fg-muted uppercase tracking-wide px-2 mb-2">
                  Recent Sessions ({sessions.length})
                </h3>
                {loadingSessions ? (
                  <div className="text-xs text-fg-muted text-center py-4">Loading sessions...</div>
                ) : sessions.length === 0 ? (
                  <div className="text-xs text-fg-muted px-2 py-4">
                    <p className="mb-2">No sessions found.</p>
                    <p className="text-micro">Click "New Chat" to start a conversation.</p>
                  </div>
                ) : (
                  <div className="space-y-1">
                    {sessions.map((sess) => (
                      <div
                        key={sess.id}
                        role="button"
                        tabIndex={0}
                        aria-current={activeSessionId === sess.id ? "true" : undefined}
                        className={cn(
                          "group relative p-2 rounded cursor-pointer transition-colors",
                          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          activeSessionId === sess.id
                            ? "bg-surface border border-brand-edge"
                            : "hover:bg-surface"
                        )}
                        onClick={() => {
                          setActiveSessionId(sess.id);
                          setSessionId(SESSION_STAGE, SESSION_AGENT, sess.id);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            setActiveSessionId(sess.id);
                            setSessionId(SESSION_STAGE, SESSION_AGENT, sess.id);
                          }
                        }}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex-1 min-w-0">
                            <div className="text-xs font-medium text-fg truncate">
                              Session {sess.id.slice(0, 8)}
                            </div>
                            <div className="text-micro text-fg-muted flex items-center gap-1 mt-1">
                              <Clock className="w-3 h-3" />
                              {new Date(sess.created_at).toLocaleDateString()}
                            </div>
                          </div>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleArchiveSession(sess.id);
                            }}
                            className="opacity-0 group-hover:opacity-100 p-1 hover:bg-red-100 rounded transition-opacity"
                            title="Archive session"
                          >
                            <Trash2 className="w-3 h-3 text-red-600" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Chat Area */}
          <div className="flex-1 flex flex-col bg-surface">
            {/* Messages */}
            <div
              ref={scrollRef}
              className="flex-1 overflow-y-auto mos-scroll p-6 space-y-4"
              role="log"
              aria-live="polite"
              aria-atomic="false"
              aria-busy={sending}
              aria-label="Conversation"
              data-testid="chat-transcript"
            >
              {!kbReady && (
                <div
                  role="status"
                  className="bg-warn-bg border border-warn-edge rounded p-4 text-sm text-fg"
                >
                  <strong className="text-warn">Knowledge base not ready.</strong>{" "}
                  Upload source files and choose Build Knowledge Base to enable
                  AI-powered discovery.
                </div>
              )}

              {history.length === 0 && kbReady && (
                <div className="text-center py-12 text-fg-muted">
                  <Bot className="w-12 h-12 mx-auto mb-3 opacity-50" />
                  <p className="text-sm">Start a conversation to explore your codebase</p>
                  <p className="text-xs mt-1">Ask questions about architecture, dependencies, or functionality</p>
                </div>
              )}

              {history.map((msg, idx) => (
                <div
                  key={idx}
                  className={cn(
                    "flex gap-3",
                    msg.role === "user" ? "justify-end" : "justify-start"
                  )}
                >
                  {msg.role === "assistant" && (
                    <div className="w-8 h-8 bg-ink rounded-full flex items-center justify-center shrink-0">
                      <Bot className="w-4 h-4 text-white" />
                    </div>
                  )}
                  <div className="flex-1 max-w-[80%]">
                    <div
                      className={cn(
                        "rounded-lg px-4 py-2",
                        msg.role === "user"
                          ? "bg-ink text-ink-fg ml-auto max-w-[90%]"
                          : "bg-surface-2 text-fg border border-border"
                      )}
                    >
                      <div className="text-sm whitespace-pre-wrap">{msg.content}</div>
                      {msg.timestamp && (
                        <div className={cn("text-micro mt-1", msg.role === "user" ? "text-fg-onDarkMuted" : "text-fg-subtle")}>
                          {new Date(msg.timestamp).toLocaleTimeString()}
                        </div>
                      )}
                    </div>
                    
                    {/* Sources this answer was grounded in. The backend
                        emits one citation per RAG chunk, so this is the
                        actual retrieval set — not a guess. */}
                    {msg.role === "assistant" && msg._citations?.length > 0 && (
                      <ul className="mt-1.5 flex flex-wrap gap-1" aria-label="Sources">
                        {msg._citations.map((c) => (
                          <li key={c.filename}>
                            <span className="inline-flex items-center gap-1 rounded-sm border border-border bg-surface-2 px-1.5 py-0.5 text-micro font-mono text-fg-muted">
                              <FileText className="size-3 shrink-0" aria-hidden />
                              {c.filename}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}

                    {msg._stopped && (
                      <p className="mt-1 text-micro text-warn">Stopped — partial reply</p>
                    )}

                    {/* Apply to SRS button for edit mode responses - Only for Discovery */}
                    {enableSrsEdit && msg.role === "assistant" && msg._editSection && (
                      <button
                        onClick={() => applySrsEdit(msg.content, msg._editSection)}
                        className="mt-2 text-xs bg-brand hover:bg-brand-hover text-fg px-3 py-1.5 rounded font-semibold transition-colors"
                      >
                        Apply to {SECTION_OPTIONS.find((s) => s.value === msg._editSection)?.label || msg._editSection}
                      </button>
                    )}
                  </div>
                  {msg.role === "user" && (
                    <div className="w-8 h-8 bg-brand rounded-full flex items-center justify-center shrink-0">
                      <User className="w-4 h-4 text-fg" />
                    </div>
                  )}
                </div>
              ))}

              {/* The assistant is working. The previous version showed three
                  bouncing dots and nothing else — no phase, no elapsed time,
                  and nothing announced to assistive tech, so a slow reply was
                  indistinguishable from a hung one. */}
              {/* In-flight reply. Tokens render as they arrive, so the
                  user reads the answer while the model is still writing it
                  — the previous version showed three bouncing dots until
                  the entire response had landed. */}
              {sending && (
                <div className="flex gap-3 justify-start" data-testid="chat-pending">
                  <div className="size-8 bg-ink rounded-lg grid place-items-center shrink-0">
                    <Bot className="size-4 text-ink-fg" aria-hidden />
                  </div>
                  <div className="flex-1 min-w-0 max-w-[80%] flex flex-col gap-1.5">
                    <div className="bg-surface-2 rounded px-4 py-2.5 border border-border flex flex-col gap-1.5">
                      {draft ? (
                        <>
                          <div className="text-sm whitespace-pre-wrap text-fg">
                            {draft}
                            <span
                              className="inline-block w-1.5 h-4 -mb-0.5 ml-0.5 bg-fg-muted motion-safe:animate-pulse"
                              aria-hidden
                            />
                          </div>
                          <span className="text-micro text-fg-subtle tabular-nums">
                            {formatDuration(thinkingFor)}
                          </span>
                        </>
                      ) : (
                        <>
                          <ThinkingDots
                            label={
                              phaseLabel(phase) ||
                              (srsEditMode ? "Drafting the section edit" : "Working")
                            }
                          />
                          <span className="text-micro text-fg-subtle tabular-nums">
                            {formatDuration(thinkingFor)} elapsed
                          </span>
                        </>
                      )}
                    </div>

                    {citations.length > 0 && (
                      <AgentTimeline
                        steps={citations.map((c) => ({
                          id: c.filename,
                          agent: "retrieving",
                          summary: c.filename,
                          status: "done",
                          source: c.filetype || undefined,
                        }))}
                      />
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* Input Area */}
            <div className="border-t border-border p-4 bg-surface">
              <div className="flex gap-2">
                <label htmlFor="chat-composer" className="sr-only">
                  Message
                </label>
                <textarea
                  id="chat-composer"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={
                    kbReady
                      ? "Ask about your codebase…"
                      : "Build the knowledge base to start chatting"
                  }
                  disabled={!kbReady || sending}
                  aria-describedby="chat-composer-hint"
                  className="flex-1 resize-none border border-border-strong rounded bg-surface text-fg px-4 py-3 text-sm placeholder:text-fg-subtle focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-surface disabled:bg-surface-2 disabled:text-fg-subtle"
                  rows={2}
                />
                {sending ? (
                  <Button
                    variant="outline"
                    onClick={handleStop}
                    aria-label="Stop generating"
                    className="px-6 self-stretch"
                    data-testid="chat-stop-btn"
                  >
                    <Square className="size-4" aria-hidden />
                  </Button>
                ) : (
                  <Button
                    variant="primary"
                    onClick={handleSend}
                    disabled={!input.trim() || !kbReady}
                    aria-label="Send message"
                    className="px-6 self-stretch"
                    data-testid="chat-send-btn"
                  >
                    <Send className="size-4" aria-hidden />
                  </Button>
                )}
              </div>
              <div className="flex items-center justify-between mt-2 text-xs text-fg-muted">
                <span id="chat-composer-hint">
                  Enter to send · Shift+Enter for a new line
                </span>
                {activeSessionId && (
                  <span className="font-mono">Session: {activeSessionId.slice(0, 8)}...</span>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
