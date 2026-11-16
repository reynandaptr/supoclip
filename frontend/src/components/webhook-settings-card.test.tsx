import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { WebhookSettingsCard } from "./webhook-settings-card";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), message: vi.fn(), error: vi.fn() } }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

const configured = {
  configured: true,
  url: "https://example.com/hook",
  enabled: true,
  secret_hint: "whsec_ab…",
  created_at: null,
  updated_at: null,
  last_delivery: { status: "delivered", attempts: 1, last_status_code: 200, created_at: null },
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.unstubAllGlobals();
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

it("shows the empty form when unconfigured", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ configured: false })));
  render(<WebhookSettingsCard />);
  expect(await screen.findByLabelText("Webhook URL")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Send test" })).not.toBeInTheDocument();
});

it("saving shows the secret once and hides it after reload", async () => {
  let saved = false;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === "PUT") {
      saved = true;
      return json({ ...configured, secret: "whsec_only-once" });
    }
    return json(saved ? configured : { configured: false });
  }));
  const { unmount } = render(<WebhookSettingsCard />);
  fireEvent.change(await screen.findByLabelText("Webhook URL"), { target: { value: "https://example.com/hook" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("whsec_only-once");
  expect(screen.getByText("Copy this signing secret now. It won't be shown again.")).toBeInTheDocument();
  expect(screen.getByText("Last delivery: delivered (200)")).toBeInTheDocument();
  unmount();
  render(<WebhookSettingsCard />);
  await screen.findByDisplayValue("https://example.com/hook");
  expect(screen.queryByText("whsec_only-once")).not.toBeInTheDocument();
});

it("send test posts to /api/webhooks/test", async () => {
  const fetchMock = vi.fn(async (url: string) =>
    url === "/api/webhooks/test" ? json({ delivery_id: "d1" }) : json(configured));
  vi.stubGlobal("fetch", fetchMock);
  render(<WebhookSettingsCard />);
  fireEvent.click(await screen.findByRole("button", { name: "Send test" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/webhooks/test", { method: "POST" }));
});

it("shows the server error for an invalid URL", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url: string, options?: RequestInit) =>
    options?.method === "PUT" ? json({ detail: "URL must use https" }, 400) : json({ configured: false })));
  render(<WebhookSettingsCard />);
  fireEvent.change(await screen.findByLabelText("Webhook URL"), { target: { value: "ftp://x" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("URL must use https")).toBeInTheDocument();
});
