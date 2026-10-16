import type {
  CitationEdgeOutput,
  CitationMapOutput,
  CitationNodeOutput,
  DiagnosticCheckOutput,
  ErrorResponse as GeneratedErrorResponse,
  DiagnosticOutput,
  DeletionReceipt as GeneratedDeletionReceipt,
  HistoryItem as GeneratedHistoryItem,
  HistoryPage as GeneratedHistoryPage,
  DiagnosticSubmission as GeneratedDiagnosticSubmission,
  PersistedEvent,
  ResultOutput,
  ResultPageOutput,
  RunAudit as GeneratedRunAudit,
  RunDetail as GeneratedRunDetail,
  RunStatus as GeneratedRunStatus,
  RunSubmission,
  SourceDescriptorOutput,
} from "./generated";

export type RunStatus = GeneratedRunStatus;
export type SourceDescriptor = SourceDescriptorOutput;
export type RunDetail = GeneratedRunDetail;
export type RunEvent = PersistedEvent;
export type RunAudit = GeneratedRunAudit;
export type PaperResult = ResultOutput;
export type ResultPage = ResultPageOutput;
export type CitationNode = CitationNodeOutput;
export type CitationEdge = CitationEdgeOutput;
export type CitationMap = CitationMapOutput;
export type SearchSubmission = RunSubmission;
/** Retry boundaries of the research pipeline, in execution order. */
export type RunStep = "query" | "retrieval" | "citation" | "selection" | "ranking" | "results";
export type DiagnosticSubmission = GeneratedDiagnosticSubmission;
export type DiagnosticCheck = DiagnosticCheckOutput;
export type ErrorResponse = GeneratedErrorResponse;
export type DiagnosticResult = DiagnosticOutput;
export type DeletionReceipt = GeneratedDeletionReceipt;
export type HistoryItem = GeneratedHistoryItem;
export type HistoryPage = GeneratedHistoryPage;
