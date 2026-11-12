import { apiGet, apiPost } from "./apiAuth";

export type UtilityNoticeKind = "interruption" | "disconnection" | "report";

export interface UtilityNotice {
  id: string;
  kind: UtilityNoticeKind;
  subject: string;
  summary: string;
  sender: string;
  receivedAt: string;
  readAt?: string | null;
  serviceArea: string | null;
  startAt: string | null;
  endAt: string | null;
  calendarSync?: "synced" | "not_scheduled" | "not_connected" | "not_configured" | "failed";
}

export interface GoogleCalendarStatus {
  configured: boolean;
  connected: boolean;
}

export interface GoogleCalendarSyncResult {
  synced: number;
  skipped: number;
  failed: number;
}

export function listUtilityNotices(): Promise<UtilityNotice[]> {
  return apiGet<UtilityNotice[]>("/api/utility-notices");
}

export function getGoogleCalendarStatus(): Promise<GoogleCalendarStatus> {
  return apiGet<GoogleCalendarStatus>("/api/google-calendar/status");
}

export function beginGoogleCalendarAuthorization(): Promise<{ authorizationUrl: string }> {
  return apiPost<{ authorizationUrl: string }>("/api/google-calendar/authorize");
}

export function syncGoogleCalendar(): Promise<GoogleCalendarSyncResult> {
  return apiPost<GoogleCalendarSyncResult>("/api/google-calendar/sync");
}

export function disconnectGoogleCalendar(): Promise<{ connected: false }> {
  return apiPost<{ connected: false }>("/api/google-calendar/disconnect");
}
