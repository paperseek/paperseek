import { reactive } from "vue";
import { api, json } from "../../api/client";
import type { components } from "../../api/generated";
import type { SourceDescriptor } from "../../api/types";

type Schema = components["schemas"];
export type Connection = Schema["ConnectionOutput"];
export type ConnectionKind = Connection["kind"];
export type ConnectionInput = Schema["ConnectionInput"];
export type SourceSetting = Schema["SourceSettingOutput"];
export type Defaults = Schema["DefaultsOutput"];
export type Probe = Schema["ProbeOutput"];
export type ProxySettings = Schema["ProxySettingsOutput"];
export type ProxyScope = ProxySettings["scope"][number];

const root = "/api/v1/settings";
const send = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/** One shared copy of saved connections, source settings and defaults. */
export const workspaceSettings = reactive({
  loaded: false,
  loading: false,
  error: false,
  connections: [] as Connection[],
  sources: [] as SourceSetting[],
  descriptors: [] as SourceDescriptor[],
  defaults: null as Defaults | null,
  proxy: null as ProxySettings | null,
});

let pending: Promise<void> | null = null;
export function loadSettings(force = false): Promise<void> {
  if (workspaceSettings.loaded && !force) return Promise.resolve();
  if (pending) return pending;
  workspaceSettings.loading = true;
  workspaceSettings.error = false;
  pending = (async () => {
    try {
      const [value, descriptors] = await Promise.all([
        json<Schema["WorkspaceSettingsOutput"]>(root),
        workspaceSettings.descriptors.length ? Promise.resolve(workspaceSettings.descriptors) : api.sources(),
      ]);
      workspaceSettings.connections = value.connections;
      workspaceSettings.sources = value.sources;
      workspaceSettings.defaults = value.defaults;
      workspaceSettings.proxy = value.proxy ?? null;
      workspaceSettings.descriptors = descriptors;
      workspaceSettings.loaded = true;
    } catch {
      workspaceSettings.error = true;
    } finally {
      workspaceSettings.loading = false;
      pending = null;
    }
  })();
  return pending;
}

export function connectionsOf(kind: ConnectionKind): Connection[] {
  return workspaceSettings.connections.filter((item) => item.kind === kind);
}
export function connectionById(id: string): Connection | undefined {
  return workspaceSettings.connections.find((item) => item.id === id);
}
export function connectionStatus(connection: Connection): "ok" | "fail" | "idle" {
  return connection.test.status === "ok" ? "ok" : connection.test.status === "failed" ? "fail" : "idle";
}
export function sourceSetting(id: string): SourceSetting | undefined {
  return workspaceSettings.sources.find((item) => item.id === id);
}

export const settingsApi = {
  create: (body: ConnectionInput) => json<Connection>(`${root}/connections`, send("POST", body)),
  update: (id: string, body: ConnectionInput) => json<Connection>(`${root}/connections/${id}`, send("PUT", body)),
  remove: (id: string, replacement = "") =>
    json<void>(`${root}/connections/${id}${replacement ? `?replacement=${encodeURIComponent(replacement)}` : ""}`, { method: "DELETE" }),
  testDraft: (body: Schema["ConnectionTestInput"]) => json<Probe>(`${root}/connections/test`, send("POST", body)),
  test: (id: string) => json<Probe>(`${root}/connections/${id}/test`, { method: "POST" }),
  saveDefaults: (body: Schema["DefaultsInput"]) => json<Defaults>(`${root}/defaults`, send("PUT", body)),
  saveSource: (id: string, body: Schema["SourceSettingsInput"]) => json<void>(`${root}/sources/${id}`, send("PUT", body)),
  testSource: (id: string) => json<Probe>(`${root}/sources/${id}/test`, { method: "POST" }),
  saveProxy: (body: Schema["ProxySettingsInput"]) => json<ProxySettings>(`${root}/proxy`, send("PUT", body)),
};

/** Select options for saved connections of one kind, with test-status dots. */
export function connectionOptions(kind: ConnectionKind) {
  return connectionsOf(kind).map((item) => ({
    value: item.id,
    label: item.model,
    desc: item.base_url.replace(/^https?:\/\//, ""),
    status: connectionStatus(item),
  }));
}

/** The saved LLM connection matching a recorded route, if any. */
export function connectionForRoute(route: { provider?: string; base_url?: string; model?: string } | undefined): string {
  if (!route) return "";
  return connectionsOf("llm").find((item) =>
    item.provider === route.provider && item.base_url === route.base_url && item.model === route.model)?.id || "";
}
