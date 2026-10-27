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
export type DiagnosticSubmission = GeneratedDiagnosticSubmission;
export type DiagnosticCheck = DiagnosticCheckOutput;
export type ErrorResponse = GeneratedErrorResponse;
export type DiagnosticResult = DiagnosticOutput;
export type DeletionReceipt = GeneratedDeletionReceipt;
export type HistoryItem = GeneratedHistoryItem;
export type HistoryPage = GeneratedHistoryPage;

export interface JournalPartitions {
  journal?: string;
  matched_by?: "issn" | "title";
  cas?: { tier: number; rank?: string; top?: boolean; category?: string; year?: number | null };
  jcr?: { quartile?: string; impact_factor?: number | null; rank?: string; category?: string; year?: number | null };
  ccf?: { rank: "A" | "B" | "C"; type?: "conference" | "journal"; abbreviation?: string; field?: string; year?: number | null };
  openalex?: { two_year_mean_citedness?: number | null; h_index?: number | null; i10_index?: number | null };
}
export interface Acceptance {
  venue?: string;
  year?: number | null;
  track?: string;
  tier?: "" | "Oral" | "Spotlight" | "Poster" | "Findings";
  label?: string;
  sources?: string[];
  url?: string;
}
