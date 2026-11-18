import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import ListPage from "./page";

vi.mock("@/lib/auth-client", () => ({
  useSession: () => ({ data: { user: { id: "user-1" } }, isPending: false }),
}));

const tasks = [
  {
    id: "old-cut",
    user_id: "user-1",
    source_id: "source-old",
    source_title: "Podcast",
    source_type: "youtube",
    source_url: "https://youtu.be/dQw4w9WgXcQ",
    status: "completed",
    clips_count: 3,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
  },
  {
    id: "standup",
    user_id: "user-1",
    source_id: "source-standup",
    source_title: "Weekly standup",
    source_type: "youtube",
    source_url: "https://www.youtube.com/watch?v=abcdefghijk",
    status: "completed",
    clips_count: 1,
    created_at: "2026-10-03T00:00:00Z",
    updated_at: "2026-10-03T00:00:00Z",
  },
  {
    id: "new-cut",
    user_id: "user-1",
    source_id: "source-new",
    source_title: "Podcast (recut)",
    source_type: "youtube",
    source_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    status: "completed",
    clips_count: 4,
    created_at: "2026-10-04T00:00:00Z",
    updated_at: "2026-10-04T00:00:00Z",
  },
];

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ tasks })));
});

it("groups repeat runs of the same original video", async () => {
  render(<ListPage />);

  expect(await screen.findByRole("link", { name: /Podcast \(recut\)/ })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { level: 2, name: "Podcast (recut)" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("tab", { name: "By video" }));

  const headings = screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
  expect(headings).toEqual(["Podcast", "Weekly standup"]);
  expect(screen.queryByText("2 generations")).not.toBeInTheDocument();

  const links = screen.getAllByRole("link")
    .map((link) => link.getAttribute("href"))
    .filter((href) => href?.startsWith("/tasks/"));
  expect(links).toEqual(["/tasks/new-cut", "/tasks/old-cut", "/tasks/standup"]);
});

describe("Send to webhook", () => {
  const mixed = [
    { ...tasks[0], id: "done-1" },
    { ...tasks[1], id: "done-2" },
    { ...tasks[2], id: "failed-1", status: "error" },
  ];

  function stubFetch(sendResponse: () => Response) {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          calls.push(url);
          return sendResponse();
        }
        return Response.json({ tasks: mixed });
      }),
    );
    return calls;
  }

  it("is disabled until a completed task is selected, and skips others", async () => {
    const calls = stubFetch(() => Response.json({ delivery_id: "d" }));
    render(<TooltipProvider><ListPage /></TooltipProvider>);
    // Only the errored task ("Podcast (recut)") selected: nothing to send.
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Podcast (recut)" }));
    expect(screen.getByRole("button", { name: /Send to webhook/ })).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: "Select all generations" }));
    expect(screen.getByRole("button", { name: /Send to webhook/ })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /Send to webhook/ }));

    expect(await screen.findByText("Sent 2 tasks to your webhook.")).toBeInTheDocument();
    expect(calls.sort()).toEqual([
      "/api/webhooks/tasks/done-1/send",
      "/api/webhooks/tasks/done-2/send",
    ]);
  });

  it("shows the server's 409 detail once", async () => {
    stubFetch(() =>
      Response.json({ detail: "Set up and enable the webhook first" }, { status: 409 }),
    );
    render(<TooltipProvider><ListPage /></TooltipProvider>);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select all generations" }));
    fireEvent.click(screen.getByRole("button", { name: /Send to webhook/ }));

    const notice = await screen.findByText(/Set up and enable the webhook first/);
    expect(notice).toBeInTheDocument();
    expect(screen.getAllByText(/Set up and enable the webhook first/)).toHaveLength(1);
  });
});
