import { NextResponse } from "next/server";

import { createTextProxyResponse, fetchBackend } from "@/server/backend-api";
import { getServerSession } from "@/server/session";

export async function POST(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const session = await getServerSession();
  if (!session?.user?.id) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { id } = await params;
  const upstream = await fetchBackend(
    `/webhooks/tasks/${encodeURIComponent(id)}/send`,
    {
      method: "POST",
      userId: session.user.id,
      cache: "no-store",
    },
  );

  return createTextProxyResponse(upstream);
}
