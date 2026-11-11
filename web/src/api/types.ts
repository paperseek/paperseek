import type {
  CitationEdgeOutput,
  CitationMapOutput,
  CitationNodeOutput,
  ErrorResponse as GeneratedErrorResponse,
  DeletionReceipt as GeneratedDeletionReceipt,
  HistoryItem as GeneratedHistoryItem,
  HistoryPage as GeneratedHistoryPage,
  PersistedEvent,
  ResultOutput,
  ResultPageOutput,
  RunAudit as GeneratedRunAudit,
  RunDetail as GeneratedRunDetail,
  RunSubmission,
  SourceDescriptorOutput,
} from "./generated";

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
export type ErrorResponse = GeneratedErrorResponse;
export type DeletionReceipt = GeneratedDeletionReceipt;
export type HistoryItem = GeneratedHistoryItem;
export type HistoryPage = GeneratedHistoryPage;

export interface JournalPartitions {
  journal?: string;
  matched_by?: "issn" | "title";
  cas?: { tier: number; rank?: string; top?: boolean; category?: string; year?: number | null };
  jcr?: { quartile?: string; impact_factor?: number | null; rank?: string; category?: string; year?: number | null };
  ccf?: { rank: "A" | "B" | "C"; type?: "conference" | "journal"; abbreviation?: string; field?: string; year?: number | null };
  // 北大核心 and the three CSSCI lists, each with its edition ("2023", "2025-2026").
  cn_core?: {
    pku?: boolean; pku_edition?: string; cssci?: boolean; cssci_edition?: string;
    cssci_collection?: boolean; cssci_collection_edition?: string; cssci_extended?: boolean; cssci_extended_edition?: string;
    former_title?: string;
  };
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
