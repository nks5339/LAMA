/**
 * Type declarations for lib/api.js — the strangler beachhead.
 *
 * GENERATED from the real exports of api.js. Regenerate after changing the
 * API surface rather than editing by hand.
 *
 * Why this file earns its place: with `allowJs` on and `checkJs` off, the
 * existing .jsx pages are not type-checked — but any .ts/.tsx module that
 * imports from "@/lib/api" is checked against THIS file. Importing a name
 * that does not exist becomes a compile error, which is precisely the bug
 * that shipped as `kbStatus`: DiscoveryV2 called a function it had never
 * imported, the call silently resolved to a local useState variable, and
 * an empty catch swallowed the TypeError. Every metric tile on the landing
 * page read 0 as a result.
 *
 * Return types are `Promise<any>` for now. Narrowing them per-endpoint is
 * the next increment; the import-time safety is what pays for itself today.
 */

/** Events emitted by POST /api/chat/stream, in the order a client sees them. */
export type ChatStreamEvent =
  | { type: "phase"; phase: "retrieving" | "thinking" | "generating" | string }
  | { type: "citation"; filename: string; filetype: string; score: number }
  | { type: "token"; text: string }
  | { type: "complete" } & ChatStreamComplete
  | { type: "error"; message: string }
  | { type: "ping" };

export interface ChatStreamComplete {
  conversation_id: string;
  message: {
    id: string;
    conversation_id: string;
    project_id: string;
    role: "assistant";
    content: string;
    model?: string;
    tokens?: number;
    created_at?: string;
  };
  intent: string | null;
  srs_triggered: boolean;
  /** Empty unless the SRS auto-trigger failed — previously swallowed. */
  srs_error: string;
  session_id: string;
  tokens: number;
}

export declare const API: string;
export declare const adminCreateTenant: (payload: Record<string, unknown>) => Promise<any>;
export declare const adminCreateUser: (payload: Record<string, unknown>) => Promise<any>;
export declare const adminDashboard: () => Promise<any>;
export declare const adminDeleteTenant: (id: string) => Promise<any>;
export declare const adminDeleteUser: (id: string) => Promise<any>;
export declare const adminListTenants: () => Promise<any>;
export declare const adminListUsers: (tenantId: string) => Promise<any>;
export declare const adminUpdateTenant: (id: string, payload: Record<string, unknown>) => Promise<any>;
export declare const adminUpdateUser: (id: string, payload: Record<string, unknown>) => Promise<any>;
export declare const analyzeSourceStack: (formData: Record<string, unknown>) => Promise<any>;
export declare const applyArchChanges: (projectId: string, changes: any, conversationMessageId: string) => Promise<any>;
export declare const applyCodegenFileChange: (projectId: string, fileId: File | Blob, newContent: any, conversationMessageId: string) => Promise<any>;
export declare const approveServiceMap: (projectId: string, approved?: any, overrides?: string, selectedServiceNames?: string, selectedUtilities?: any) => Promise<any>;
export declare const archiveSession: (sessionId: string, reason?: any) => Promise<any>;
export declare const buildKB: (projectId: string, opts?: Record<string, unknown>) => Promise<any>;
export declare const cancelCodegenMultiAgent: (pid: string) => Promise<any>;
export declare const cancelLivingJob: (jobId: string) => Promise<any>;
export declare const cancelSRSGeneration: (projectId: string) => Promise<any>;
export declare const cloneGitRepo: (projectId: string, payload: Record<string, unknown>, replace?: any) => Promise<any>;
export declare const cloneGitRepoAndWait: (
  projectId: string,
  payload: Record<string, unknown>,
  replace?: boolean,
  opts?: { intervalMs?: number; timeoutMs?: number; onTick?: (s: unknown) => void },
) => Promise<any>;
export declare const cloneGitStatus: (projectId: string, sourceId?: string) => Promise<any>;
export declare const confirmCodegenMultiAgentEnvelopes: (pid: string, model?: string) => Promise<any>;
export declare const confirmCodegenMultiAgentTasks: (pid: string, model?: string) => Promise<any>;
export declare const confirmTransformationPlan: (transformId: string, model?: string) => Promise<any>;
export declare const confirmTransformationTasks: (transformId: string, model?: string) => Promise<any>;
export declare const confirmTransformationTraceability: (transformId: string, model?: string) => Promise<any>;
export declare const createGapAnalysis: (formData: Record<string, unknown>, opts?: Record<string, unknown>) => Promise<any>;
export declare const createProject: (payload: Record<string, unknown>) => Promise<any>;
export declare const createSession: (payload: Record<string, unknown>) => Promise<any>;
export declare const createTransformation: (formData: Record<string, unknown>, opts?: Record<string, unknown>) => Promise<any>;
export declare const dbConnect: (projectId: string, payload: Record<string, unknown>) => Promise<any>;
export declare const deleteCodegenFile: (projectId: string, fileId: File | Blob) => Promise<any>;
export declare const deleteCodegenPath: (projectId: string, pathPrefix: string, serviceName: string) => Promise<any>;
export declare const deleteFactoryOrchestratorConfig: (projectId: string) => Promise<any>;
export declare const deleteGapAnalysis: (analysisId: string) => Promise<any>;
export declare const deleteKBFile: (fileId: File | Blob) => Promise<any>;
export declare const deleteProject: (projectId: string) => Promise<any>;
export declare const deleteProvider: (id: string) => Promise<any>;
export declare const deleteTransformation: (transformId: string) => Promise<any>;
export declare const downloadArchArtifactPdfUrl: (projectId: string, artifactId: string) => Promise<any>;
export declare const downloadArchArtifactUrl: (projectId: string, artifactId: string) => Promise<any>;
export declare const downloadArtifactUrl: (projectId: string, artifactId: string) => Promise<any>;
export declare const downloadLivingArtifactUrl: (projectId: string, artId: string) => Promise<any>;
export declare const downloadTestCasesExcelUrl: (projectId: string, artId: string) => Promise<any>;
export declare const downloadTransformedBundle: (transformId: string) => Promise<any>;
export declare const downloadTransformedCode: (transformId: string, scope?: any) => Promise<any>;
export declare const downloadTransformedTests: (transformId: string) => Promise<any>;
export declare const ensureFactoryOrchestratorWorkspace: (projectId: string) => Promise<any>;
export declare const exportCodegenToDisk: (projectId: string) => Promise<any>;
export declare const factoryReset: (projectId: string) => Promise<any>;
export declare const fetchProviderModels: (id: string) => Promise<any>;
export declare const freezeArchArtifact: (projectId: string, artifactId: string) => Promise<any>;
export declare const freezeArtifact: (projectId: string, artifactId: string) => Promise<any>;
export declare const freezeCodegen: (projectId: string) => Promise<any>;
export declare const freezeGapAnalysis: (analysisId: string) => Promise<any>;
export declare const freezeLiving: (projectId: string) => Promise<any>;
export declare const freezeLivingArtifact: (projectId: string, artId: string) => Promise<any>;
export declare const freezeSRS: (projectId: string, user: any, override: string) => Promise<any>;
export declare const gapAnalysisExportUrl: (analysisId: string, format: any) => Promise<any>;
export declare const generateBusMatrix: (projectId: string, model: string) => Promise<any>;
export declare const generateEntityGraph: (projectId: string) => Promise<any>;
export declare const getAgentTimeline: (transformId: string) => Promise<any>;
export declare const getArchArtifacts: (projectId: string) => Promise<any>;
export declare const getArchJob: (jobId: string) => Promise<any>;
export declare const getArtifact: (projectId: string, artifactId: string) => Promise<any>;
export declare const getAuditTrace: (traceId: string) => Promise<any>;
export declare const getBusinessOntology: (projectId: string) => Promise<any>;
export declare const getBusinessOntologyJob: (projectId: string, jobId: string) => Promise<any>;
export declare const getCodegenApiMapping: (projectId: string) => Promise<any>;
export declare const getCodegenExportRoot: (projectId: string) => Promise<any>;
export declare const getCodegenFile: (projectId: string, fileId: File | Blob) => Promise<any>;
export declare const getCodegenJob: (jobId: string) => Promise<any>;
export declare const getCodegenMultiAgentState: (pid: string) => Promise<any>;
export declare const getCodegenMultiAgentTraceability: (pid: string) => Promise<any>;
export declare const getCompilationResult: (transformId: string) => Promise<any>;
export declare const getConfidenceJob: (projectId: string, jobId: string) => Promise<any>;
export declare const getDataModelArtifacts: (projectId: string) => Promise<any>;
export declare const getDataModelJob: (jobId: string) => Promise<any>;
export declare const getFactoryOrchestratorConfig: (projectId: string) => Promise<any>;
export declare const getGapAnalysis: (analysisId: string) => Promise<any>;
export declare const getGapAnalysisKB: (analysisId: string) => Promise<any>;
export declare const getGitSource: (projectId: string) => Promise<any>;
export declare const getGithubConfig: (projectId: string) => Promise<any>;
export declare const getJourneySettings: (projectId: string) => Promise<any>;
export declare const getLatestAccuracyReport: (projectId: string) => Promise<any>;
export declare const getLivingArtifact: (projectId: string, artId: string) => Promise<any>;
export declare const getLivingJob: (jobId: string) => Promise<any>;
export declare const getParityReport: (projectId: string, runId: string) => Promise<any>;
export declare const getPipelineStatus: (projectId: string) => Promise<any>;
export declare const getProjectIntegrations: (projectId: string) => Promise<any>;
export declare const getProjectSettings: (projectId: string) => Promise<any>;
export declare const getSRS: (projectId: string) => Promise<any>;
export declare const getSRSGenerateStatus: (projectId: string) => Promise<any>;
export declare const getSession: (sessionId: string) => Promise<any>;
export declare const getStageConfidence: (projectId: string, stage: string) => Promise<any>;
export declare const getTargetStackSuggestions: (projectId: string, topN?: any) => Promise<any>;
export declare const getTransformation: (transformId: string) => Promise<any>;
export declare const getTransformationEnvelopes: (transformId: string) => Promise<any>;
export declare const getTransformationFile: (transformId: string, fileId: File | Blob) => Promise<any>;
export declare const getTransformationFiles: (transformId: string, fileType?: File | Blob) => Promise<any>;
export declare const getTransformationLogs: (transformId: string, opts?: Record<string, unknown>, limit?: number) => Promise<any>;
export declare const getTransformationStatus: (transformId: string) => Promise<any>;
export declare const getTransformationTasks: (transformId: string) => Promise<any>;
export declare const getTransformationTraceability: (transformId: string) => Promise<any>;
export declare const getTransformerAgentConfig: (transformId: string, agent: any) => Promise<any>;
export declare const getTransformerKB: (transformId: string) => Promise<any>;
export declare const getUsageSummary: (projectId: string, days?: any) => Promise<any>;
export declare const injectIntegrations: (projectId: string, language?: any) => Promise<any>;
export declare const kbBuildProgress: (projectId: string) => Promise<any>;
export declare const kbStatus: (projectId: string) => Promise<any>;
export declare const listAgents: () => Promise<any>;
export declare const listArchUtilities: (projectId: string) => Promise<any>;
export declare const listAudit: (projectId: string) => Promise<any>;
export declare const listCodegenFiles: (projectId: string) => Promise<any>;
export declare const listCodegenMultiAgentEnvelopes: (pid: string) => Promise<any>;
export declare const listCodegenMultiAgentRuns: (pid: string, opts?: Record<string, unknown>) => Promise<any>;
export declare const listCodegenMultiAgentTasks: (pid: string, wave?: any) => Promise<any>;
export declare const listDataSources: (projectId: string) => Promise<any>;
export declare const listGapAnalyses: () => Promise<any>;
export declare const listKBFiles: (projectId: string) => Promise<any>;
export declare const listLivingArtifacts: (projectId: string) => Promise<any>;
export declare const listModels: () => Promise<any>;
export declare const listProjectPrompts: (projectId: string) => Promise<any>;
export declare const listProjects: () => Promise<any>;
export declare const listPrompts: () => Promise<any>;
export declare const listProviders: () => Promise<any>;
export declare const listSessions: (
  projectId: string,
  opts?: { stage?: string; agentKey?: string; includeArchived?: boolean },
) => Promise<any>;
export declare const listTransformations: (opts?: Record<string, unknown>) => Promise<any>;
export declare const listTransformerAgentConfigs: (transformId: string) => Promise<any>;
export declare const login: (username: string, password: any) => Promise<any>;
export declare const logout: () => Promise<any>;
export declare const me: () => Promise<any>;
export declare const mergeServices: (projectId: string, serviceNames: string, mergedName: string, mergedDisplayName?: string, mergedDescription?: any) => Promise<any>;
export declare const mergeServicesBatch: (projectId: string, groups: any) => Promise<any>;
export declare const pauseCodegenJob: (jobId: string) => Promise<any>;
export declare const pauseConfidenceJob: (projectId: string, jobId: string) => Promise<any>;
export declare const pauseSRSGeneration: (projectId: string) => Promise<any>;
export declare const pauseTransformation: (transformId: string) => Promise<any>;
export declare const pollGapAnalysisStatus: (analysisId: string) => Promise<any>;
export declare const previewPrompt: (promptKey: string, projectId: string) => Promise<any>;
export declare const purgeBrokenArch: (projectId: string, types: any) => Promise<any>;
export declare const pushTransformationToGithub: (transformId: string, formData: Record<string, unknown>) => Promise<any>;
export declare const recomputeStageConfidence: (projectId: string, stage: string, opts?: Record<string, unknown>, fast?: any) => Promise<any>;
export declare const regenerateSRSSection: (projectId: string, sectionKey: string, opts?: Record<string, unknown>, conversationId?: string) => Promise<any>;
export declare const regenerateSRSSectionStream: (
  projectId: string,
  sectionKey: string,
  opts?: { model?: string; conversationId?: string },
  onEvent?: (evt: Record<string, unknown>) => void,
) => Promise<Record<string, unknown>>;
export declare const regenerateTransformationFile: (transformId: string, fileId: File | Blob, model?: string) => Promise<any>;
export declare const registerAppUrl: (projectId: string, application_url: string, notes?: any) => Promise<any>;
export declare const rerunCodegenMultiAgent: (pid: string, model?: string, opts?: Record<string, unknown>) => Promise<any>;
export declare const rerunTransformerPipeline: (transformId: string) => Promise<any>;
export declare const resetAgentBudget: (key: string) => Promise<any>;
export declare const resetArch: (projectId: string) => Promise<any>;
export declare const resetCodegen: (projectId: string) => Promise<any>;
export declare const resetLiving: (projectId: string) => Promise<any>;
export declare const resetSRS: (projectId: string, confirm?: any) => Promise<any>;
export declare const resetStage2: (projectId: string) => Promise<any>;
export declare const resumeCodegenJob: (jobId: string) => Promise<any>;
export declare const resumeConfidenceJob: (projectId: string, jobId: string) => Promise<any>;
export declare const resumeSRSGeneration: (projectId: string) => Promise<any>;
export declare const resumeTransformation: (transformId: string) => Promise<any>;
export declare const retryCodegenMultiAgentPlanner: (pid: string, model?: string) => Promise<any>;
export declare const runCompilationAnalysis: (transformId: string, model?: string, opts?: Record<string, unknown>) => Promise<any>;
export declare const runGapAnalysis: (analysisId: string, model?: string) => Promise<any>;
export declare const runMultiAgentTransformation: (transformId: string, model?: string) => Promise<any>;
export declare const runTransformation: (transformId: string, model?: string) => Promise<any>;
export declare const saveTransformerAgentConfig: (transformId: string, agent: any, payload: Record<string, unknown>) => Promise<any>;
export declare const scanFolder: (projectId: string, folderPath: string, kind?: any, replace?: any) => Promise<any>;
export declare const selectTargetStack: (projectId: string, payload: Record<string, unknown>) => Promise<any>;
export declare const sendAgentPlanChat: (transformId: string, panel: any, message: string, model?: string) => Promise<any>;
export declare const sendArchChat: (payload: Record<string, unknown>) => Promise<any>;
export declare const sendCodegenChat: (payload: Record<string, unknown>) => Promise<any>;
export declare const sendMessage: (payload: Record<string, unknown>) => Promise<any>;
export declare const setCodegenMultiAgentBuildSystem: (pid: string, be: any, fe: any) => Promise<any>;
export declare const setProjectIntegration: (projectId: string, integrationId: string, enabled: boolean, configOverrides?: string) => Promise<any>;
export declare const setupProvider: (data: Record<string, unknown>) => Promise<any>;
export declare const skipStage: (projectId: string, stage: string, opts?: Record<string, unknown>) => Promise<any>;
export declare const srsPdfUrl: (projectId: string) => Promise<any>;
export declare const startAccuracyReport: (projectId: string, sections?: string) => Promise<any>;
export declare const startArchApiContracts: (projectId: string, model: string) => Promise<any>;
export declare const startArchHld: (projectId: string, model: string) => Promise<any>;
export declare const startArchLld: (projectId: string, model: string) => Promise<any>;
export declare const startArchRecommend: (projectId: string, model: string, message: string) => Promise<any>;
export declare const startArchSequence: (projectId: string, model: string) => Promise<any>;
export declare const startAutoValidate: (projectId: string, opts?: Record<string, unknown>) => Promise<any>;
export declare const startBusinessOntologyJob: (projectId: string, force?: boolean) => Promise<any>;
export declare const startCodegenJob: (projectId: string, model: string, serviceNameOrList: string) => Promise<any>;
export declare const startCodegenMultiAgent: (pid: string, model?: string) => Promise<any>;
export declare const startCodegenZipDownload: (projectId: string) => Promise<any>;
export declare const startGapRecoveryBackend: (projectId: string, model: string, serviceNameOrList: string) => Promise<any>;
export declare const startGapRecoveryFrontend: (projectId: string, model: string, serviceNameOrList: string) => Promise<any>;
export declare const startGithubPushJob: (projectId: string) => Promise<any>;
export declare const startLivingJob: (kind: any, projectId: string, extra?: any) => Promise<any>;
export declare const startOLAPJob: (projectId: string, model: string) => Promise<any>;
export declare const startOLTPJob: (projectId: string, model: string) => Promise<any>;
export declare const startScriptsJob: (projectId: string, model: string) => Promise<any>;
export declare const startTestCases: (projectId: string, model?: string) => Promise<any>;
export declare const stopCodegenJob: (jobId: string) => Promise<any>;
export declare const stopConfidenceJob: (projectId: string, jobId: string) => Promise<any>;
export declare const stopTransformation: (transformId: string) => Promise<any>;
export declare const streamMessage: (
  payload: Record<string, unknown>,
  onEvent?: (evt: ChatStreamEvent) => void,
  signal?: AbortSignal,
) => Promise<ChatStreamComplete>;
export declare const suggestBuildTools: (params: any) => Promise<any>;
export declare const tailBackendLogs: (opts?: Record<string, unknown>, limit?: number, minLevel?: any, contains?: any) => Promise<any>;
export declare const testAgent: (key: string, projectId: string) => Promise<any>;
export declare const testFactoryOrchestratorCli: (cliBin: any) => Promise<any>;
export declare const testFactoryOrchestratorConfig: (projectId: string) => Promise<any>;
export declare const testPrompt: (promptKey: string, projectId: string, modelOverride: string) => Promise<any>;
export declare const testProvider: (id: string) => Promise<any>;
export declare const unfreezeGapAnalysis: (analysisId: string) => Promise<any>;
export declare const unfreezeSRS: (projectId: string) => Promise<any>;
export declare const unmergeService: (projectId: string, mergedName: string) => Promise<any>;
export declare const unskipStage: (projectId: string, stage: string) => Promise<any>;
export declare const updateAgent: (key: string, data: Record<string, unknown>) => Promise<any>;
export declare const updateArchArtifact: (projectId: string, artifactId: string, content: any) => Promise<any>;
export declare const updateArtifact: (projectId: string, artifactId: string, content: any) => Promise<any>;
export declare const updateCodegenFile: (projectId: string, fileId: File | Blob, content: any) => Promise<any>;
export declare const updateCodegenMultiAgentEnvelope: (pid: string, envId: string, patch: any) => Promise<any>;
export declare const updateCodegenMultiAgentTask: (pid: string, taskId: string, patch: any) => Promise<any>;
export declare const updateFactoryOrchestratorConfig: (payload: Record<string, unknown>) => Promise<any>;
export declare const updateJourneySettings: (projectId: string, patch: any) => Promise<any>;
export declare const updateLivingArtifact: (projectId: string, artId: string, files: File | Blob) => Promise<any>;
export declare const updateProjectPrompt: (projectId: string, key: string, payload: Record<string, unknown>) => Promise<any>;
export declare const updateProjectSettings: (projectId: string, patch: any) => Promise<any>;
export declare const updatePrompt: (key: string, payload: Record<string, unknown>) => Promise<any>;
export declare const updateProvider: (id: string, data: Record<string, unknown>) => Promise<any>;
export declare const updateProviderKey: (id: string, apiKey: string) => Promise<any>;
export declare const updateSRSSection: (projectId: string, section: string, content: any) => Promise<any>;
export declare const uploadKBFiles: (projectId: string, files: File | Blob, kind?: any, replace?: any) => Promise<any>;
export declare const wakeFactoryOrchestratorDroid: (projectId: string) => Promise<any>;
