import React, { useState, useEffect, useRef, useCallback } from "react";
import { X, MessageSquare, Send, Bot, User, Plus, Archive, Clock, Trash2, Pencil } from "lucide-react";
import { chatHistory, sendMessage, createSession, getSession, listSessions, archiveSession, updateSRSSection } from "@/lib/api";
import { useProjects } from "@/state/ProjectContext";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

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
  const [activeSession, setActiveSession] = useState(null);
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
        console.log("Loading session:", activeSessionId);
        // Get session details which contain live_turns
        const sess = await getSession(activeSessionId);
        console.log("Session loaded:", sess);
        setActiveSession(sess);
        
        // Convert session's live_turns to chat history format
        if (sess?.live_turns && Array.isArray(sess.live_turns)) {
          console.log("Live turns found:", sess.live_turns.length);
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
          console.log("Converted messages:", messages);
          setHistory(messages);
        } else {
          console.log("No live turns in session");
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
  }, [history]);

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

    // Optimistic update
    const tempMsg = { role: "user", content: userMsg, timestamp: new Date().toISOString() };
    setHistory((prev) => [...prev, tempMsg]);

    try {
      // Create session on first message if none exists
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
      
      // Only add edit mode params for Discovery chat
      if (enableSrsEdit) {
        payload.edit_mode = srsEditMode;
        payload.selected_section = srsEditMode ? editSection : null;
      }
      
      const resp = await sendMessage(payload);

      // If a session was used, reload it to get updated live_turns
      if (sessionIdToUse) {
        const sess = await getSession(sessionIdToUse);
        setActiveSession(sess);
        
        // Convert session's live_turns to history
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
              // Tag edit mode responses for Apply button (only for Discovery)
              const msg = {
                role: "assistant",
                content: turn.assistant_content,
                timestamp: turn.timestamp || sess.updated_at
              };
              if (enableSrsEdit && srsEditMode && turn.metadata?.edit_section) {
                msg._editSection = turn.metadata.edit_section;
              }
              messages.push(msg);
            }
          });
          setHistory(messages);
        }
      } else {
        // Fallback to old conversation-based history
        const enrichedMsg = (enableSrsEdit && srsEditMode)
          ? { role: "assistant", content: resp.response, timestamp: resp.timestamp, _editSection: editSection }
          : { role: "assistant", content: resp.response, timestamp: resp.timestamp };
          
        setHistory((prev) => [
          ...prev.filter((m) => m !== tempMsg),
          { role: "user", content: userMsg, timestamp: resp.timestamp },
          enrichedMsg,
        ]);
      }

      setConversationId(resp.conversation_id);
      if (onConversationUpdated) {
        onConversationUpdated(resp.conversation_id, resp.srs_triggered || false);
      }
      
      // Reload sessions if we just created a new one
      if (!activeSessionId && sessionIdToUse) {
        loadSessions();
      }
    } catch (e) {
      toast.error("Message failed", {
        description: e.response?.data?.detail || e.message,
      });
      setHistory((prev) => prev.filter((m) => m !== tempMsg));
    } finally {
      setSending(false);
    }
  };

  const handleKeyPress = (e) => {
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
        className="fixed bottom-6 right-6 w-14 h-14 bg-[#2E2E38] hover:bg-[#FFE600] text-white hover:text-[#2E2E38] rounded-full shadow-lg flex items-center justify-center transition-all hover:scale-110 z-50"
        aria-label="Open Discovery Chat"
        data-testid="floating-chat-trigger"
      >
        <MessageSquare className="w-6 h-6" />
      </button>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="bg-white rounded-lg shadow-2xl w-[95vw] h-[90vh] max-w-7xl flex flex-col overflow-hidden">
        {/* Header */}
        <div className="bg-[#2E2E38] text-white px-6 py-3 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-3">
            <MessageSquare className="w-5 h-5" />
            <div>
              <h2 className="font-display text-lg font-bold">{chatTitle}</h2>
              <p className="text-xs text-gray-300">
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
                    ? "bg-[#FFE600] text-[#2E2E38] font-semibold"
                    : "bg-white/10 text-white hover:bg-white/20"
                )}
              >
                <Pencil className="w-3 h-3" />
                SRS Edit
              </button>
            )}
            
            <button
              onClick={() => setShowSidebar(!showSidebar)}
              className="hover:bg-white/10 rounded px-3 py-1.5 transition-colors text-xs"
              title={showSidebar ? "Hide sessions" : "Show sessions"}
            >
              {showSidebar ? "Hide Sessions" : "Show Sessions"}
            </button>
            <button
              onClick={() => setIsOpen(false)}
              className="hover:bg-white/10 rounded p-2 transition-colors"
              aria-label="Close chat"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* SRS Edit Mode strip - Only for Discovery */}
        {enableSrsEdit && srsEditMode && (
          <div className="px-6 py-2 bg-[#FFE600]/10 border-b border-[#FFE600]/40 flex items-center gap-3">
            <span className="text-[10px] uppercase tracking-wider text-[#2E2E38] font-semibold">Editing section:</span>
            <select
              value={editSection}
              onChange={(e) => setEditSection(e.target.value)}
              className="text-xs border border-[#E6E6E6] rounded px-2 py-1 bg-white focus:border-[#2E2E38] focus:ring-1 focus:ring-[#2E2E38] outline-none"
            >
              {SECTION_OPTIONS.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
            <span className="text-[10px] text-[#747480]">Describe what to add or change in this section.</span>
          </div>
        )}
        
        {/* Category tabs - For DataModel chat */}
        {categories && categories.length > 0 && (
          <div className="border-b border-[#E6E6E6] flex bg-white">
            {categories.map((cat) => (
              <button
                key={cat.key}
                type="button"
                onClick={() => setActiveCategory(cat.key)}
                className={`text-xs px-4 py-2 font-semibold tracking-tight border-r border-[#E6E6E6] transition-colors ${
                  activeCategory === cat.key
                    ? "bg-[#2E2E38] text-white"
                    : "bg-white text-[#747480] hover:text-[#2E2E38] hover:bg-gray-50"
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
            <div className="w-64 bg-[#F6F6FA] border-r border-[#E6E6E6] flex flex-col shrink-0">
              <div className="p-4 border-b border-[#E6E6E6]">
                <button
                  onClick={handleNewSession}
                  className="w-full bg-[#FFE600] hover:bg-[#FFD700] text-[#2E2E38] font-semibold py-2 px-4 rounded-lg flex items-center justify-center gap-2 transition-colors"
                >
                  <Plus className="w-4 h-4" />
                  New Chat
                </button>
              </div>

              <div className="flex-1 overflow-y-auto p-2">
                <h3 className="text-xs font-semibold text-[#747480] uppercase tracking-wide px-2 mb-2">
                  Recent Sessions ({sessions.length})
                </h3>
                {loadingSessions ? (
                  <div className="text-xs text-[#747480] text-center py-4">Loading sessions...</div>
                ) : sessions.length === 0 ? (
                  <div className="text-xs text-[#747480] px-2 py-4">
                    <p className="mb-2">No sessions found.</p>
                    <p className="text-[10px]">Click "New Chat" to start a conversation.</p>
                  </div>
                ) : (
                  <div className="space-y-1">
                    {sessions.map((sess) => (
                      <div
                        key={sess.id}
                        className={cn(
                          "group relative p-2 rounded-lg cursor-pointer transition-colors",
                          activeSessionId === sess.id
                            ? "bg-white border border-[#FFE600]"
                            : "hover:bg-white/50"
                        )}
                        onClick={() => {
                          setActiveSessionId(sess.id);
                          setSessionId(SESSION_STAGE, SESSION_AGENT, sess.id);
                        }}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex-1 min-w-0">
                            <div className="text-xs font-medium text-[#2E2E38] truncate">
                              Session {sess.id.slice(0, 8)}
                            </div>
                            <div className="text-[10px] text-[#747480] flex items-center gap-1 mt-1">
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
          <div className="flex-1 flex flex-col bg-white">
            {/* Messages */}
            <div ref={scrollRef} className="flex-1 overflow-y-auto p-6 space-y-4">
              {!kbReady && (
                <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 text-sm text-amber-900">
                  <strong>Knowledge Base not ready.</strong> Build the KB first to enable AI-powered discovery.
                </div>
              )}

              {history.length === 0 && kbReady && (
                <div className="text-center py-12 text-[#747480]">
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
                    <div className="w-8 h-8 bg-[#2E2E38] rounded-full flex items-center justify-center shrink-0">
                      <Bot className="w-4 h-4 text-white" />
                    </div>
                  )}
                  <div className="flex-1 max-w-[80%]">
                    <div
                      className={cn(
                        "rounded-lg px-4 py-2",
                        msg.role === "user"
                          ? "bg-[#2E2E38] text-white ml-auto max-w-[90%]"
                          : "bg-[#F6F6FA] text-[#2E2E38] border border-[#E6E6E6]"
                      )}
                    >
                      <div className="text-sm whitespace-pre-wrap">{msg.content}</div>
                      {msg.timestamp && (
                        <div className={cn("text-[10px] mt-1", msg.role === "user" ? "text-gray-300" : "text-[#747480]")}>
                          {new Date(msg.timestamp).toLocaleTimeString()}
                        </div>
                      )}
                    </div>
                    
                    {/* Apply to SRS button for edit mode responses - Only for Discovery */}
                    {enableSrsEdit && msg.role === "assistant" && msg._editSection && (
                      <button
                        onClick={() => applySrsEdit(msg.content, msg._editSection)}
                        className="mt-2 text-xs bg-[#FFE600] hover:bg-[#FFD700] text-[#2E2E38] px-3 py-1.5 rounded font-semibold transition-colors"
                      >
                        Apply to {SECTION_OPTIONS.find((s) => s.value === msg._editSection)?.label || msg._editSection}
                      </button>
                    )}
                  </div>
                  {msg.role === "user" && (
                    <div className="w-8 h-8 bg-[#FFE600] rounded-full flex items-center justify-center shrink-0">
                      <User className="w-4 h-4 text-[#2E2E38]" />
                    </div>
                  )}
                </div>
              ))}

              {sending && (
                <div className="flex gap-3 justify-start">
                  <div className="w-8 h-8 bg-[#2E2E38] rounded-full flex items-center justify-center shrink-0">
                    <Bot className="w-4 h-4 text-white" />
                  </div>
                  <div className="bg-[#F6F6FA] rounded-lg px-4 py-2 border border-[#E6E6E6]">
                    <div className="flex gap-1">
                      <div className="w-2 h-2 bg-[#747480] rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                      <div className="w-2 h-2 bg-[#747480] rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                      <div className="w-2 h-2 bg-[#747480] rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* Input Area */}
            <div className="border-t border-[#E6E6E6] p-4 bg-white">
              <div className="flex gap-2">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyPress={handleKeyPress}
                  placeholder="Ask about your codebase..."
                  disabled={!kbReady || sending}
                  className="flex-1 resize-none border border-[#E6E6E6] rounded-lg px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-[#FFE600] disabled:bg-gray-50 disabled:text-gray-400"
                  rows={2}
                />
                <button
                  onClick={handleSend}
                  disabled={!input.trim() || sending || !kbReady}
                  className="bg-[#2E2E38] hover:bg-[#FFE600] text-white hover:text-[#2E2E38] px-6 rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center"
                >
                  <Send className="w-5 h-5" />
                </button>
              </div>
              <div className="flex items-center justify-between mt-2 text-xs text-[#747480]">
                <span>Press Enter to send, Shift+Enter for new line</span>
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
