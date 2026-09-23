let token = "";
export const taskTitle = (task: { id: string; options: { visual_project?: string | null } }) =>
  `Task ${task.options.visual_project?.replace(/[\\/]+$/, "") || task.id.slice(0, 8)}`;
export const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
export const setToken = (value: string) => {
  token = value;
};
export async function api<T>(
  path: string,
  method = "GET",
  data?: unknown,
  extraHeaders: Record<string, string> = {},
): Promise<T> {
  const response = await fetch(`${import.meta.env.DEV ? "/api" : ""}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...extraHeaders,
    },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `Request failed (${response.status}): ${method} ${path.split("?")[0]}`,
    );
  }
  return response.json();
}
export const pretty = (value: unknown) => JSON.stringify(value, null, 2);
export const terminal = (status: string) =>
  [
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "waiting_for_approval",
    "paused",
    "stalled",
  ].includes(status);
