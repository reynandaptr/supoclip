"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";
import { AlertCircle, Check, Copy } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useCopyText } from "@/lib/copy-text";

interface LastDelivery {
  status: string;
  attempts: number;
  last_status_code: number | null;
  created_at: string | null;
}

interface WebhookState {
  configured: boolean;
  url?: string;
  enabled?: boolean;
  secret_hint?: string;
  last_delivery?: LastDelivery | null;
}

async function errorDetail(response: Response, fallback: string): Promise<string> {
  const data = await response.json().catch(() => ({}));
  return typeof data?.detail === "string" && data.detail ? data.detail : fallback;
}

export function WebhookSettingsCard() {
  const t = useTranslations("settings");
  const copyText = useCopyText();
  const secretRef = useRef<HTMLElement>(null);
  const [webhook, setWebhook] = useState<WebhookState | null>(null);
  const [url, setUrl] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [secret, setSecret] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = useCallback((data: WebhookState) => {
    setWebhook(data);
    setUrl(data.configured ? data.url ?? "" : "");
    setEnabled(data.configured ? data.enabled ?? true : true);
  }, []);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/webhooks", { cache: "no-store" });
      if (!response.ok) throw new Error(await errorDetail(response, t("webhook.loadFailed")));
      apply(await response.json());
    } catch (err) {
      setWebhook({ configured: false });
      setError(err instanceof Error ? err.message : t("webhook.loadFailed"));
    }
  }, [apply, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (fallback: string, action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : fallback);
    } finally {
      setBusy(false);
    }
  };

  const handleSave = () =>
    run(t("webhook.saveFailed"), async () => {
      const response = await fetch("/api/webhooks", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim(), enabled }),
      });
      if (!response.ok) throw new Error(await errorDetail(response, t("webhook.saveFailed")));
      const data = await response.json();
      apply(data);
      if (typeof data.secret === "string") {
        setSecret(data.secret);
        setCopied(false);
      }
    });

  const handleTest = () =>
    run(t("webhook.testFailed"), async () => {
      const response = await fetch("/api/webhooks/test", { method: "POST" });
      if (!response.ok) throw new Error(await errorDetail(response, t("webhook.testFailed")));
      toast.success(t("webhook.testQueued"));
    });

  const handleRotate = () =>
    run(t("webhook.rotateFailed"), async () => {
      const response = await fetch("/api/webhooks/rotate-secret", { method: "POST" });
      if (!response.ok) throw new Error(await errorDetail(response, t("webhook.rotateFailed")));
      const data = await response.json();
      setSecret(data.secret ?? null);
      setCopied(false);
    });

  const handleRemove = () =>
    run(t("webhook.removeFailed"), async () => {
      const response = await fetch("/api/webhooks", { method: "DELETE" });
      if (!response.ok) throw new Error(await errorDetail(response, t("webhook.removeFailed")));
      setSecret(null);
      apply({ configured: false });
    });

  const handleCopy = async () => {
    if (!secret) return;
    const outcome = await copyText(secret, secretRef.current);
    if (outcome !== "copied") return;
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const last = webhook?.last_delivery;

  return (
    <section className="mt-6 overflow-hidden rounded-xl border bg-card" aria-labelledby="webhook-title">
      <div className="p-5">
        <h2 id="webhook-title" className="text-sm font-semibold">{t("webhook.title")}</h2>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{t("webhook.description")}</p>
      </div>

      <div className="grid gap-4 border-t p-5">
        {webhook === null ? (
          <Skeleton className="h-12 w-full" />
        ) : (
          <>
            {error && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            {secret && (
              <Alert>
                <AlertDescription>
                  <p className="font-medium">{t("webhook.secretNotice")}</p>
                  <div className="mt-2 flex min-w-0 items-center gap-2">
                    <code ref={secretRef} tabIndex={-1} className="min-w-0 flex-1 select-text break-all rounded-md border bg-muted px-3 py-2 font-mono text-sm">
                      {secret}
                    </code>
                    <Button size="icon" variant="outline" onClick={handleCopy} aria-label={t("webhook.copySecret")}>
                      {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
                    </Button>
                  </div>
                </AlertDescription>
              </Alert>
            )}

            <form
              className="grid gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                void handleSave();
              }}
            >
              <div className="grid gap-2">
                <Label htmlFor="webhook-url">{t("webhook.urlLabel")}</Label>
                <Input
                  id="webhook-url"
                  type="url"
                  inputMode="url"
                  placeholder={t("webhook.urlPlaceholder")}
                  value={url}
                  maxLength={2048}
                  onChange={(e) => setUrl(e.target.value)}
                  disabled={busy}
                />
              </div>
              <div className="flex items-center gap-2">
                <input id="webhook-enabled" type="checkbox" className="size-4 accent-primary" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} disabled={busy} />
                <Label htmlFor="webhook-enabled">{t("webhook.enabled")}</Label>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button type="submit" size="sm" disabled={busy || !url.trim()}>
                  {busy ? t("webhook.saving") : t("webhook.save")}
                </Button>
                {webhook.configured && (
                  <>
                    <Button type="button" size="sm" variant="outline" onClick={handleTest} disabled={busy}>
                      {t("webhook.sendTest")}
                    </Button>
                    <Button type="button" size="sm" variant="outline" onClick={handleRotate} disabled={busy}>
                      {t("webhook.rotate")}
                    </Button>
                    <Button type="button" size="sm" variant="ghost" onClick={handleRemove} disabled={busy}>
                      {t("webhook.remove")}
                    </Button>
                  </>
                )}
              </div>
            </form>

            {webhook.configured && (
              <div className="space-y-1 text-xs text-muted-foreground">
                {webhook.secret_hint && <p>{t("webhook.secretHint", { hint: webhook.secret_hint })}</p>}
                <p>
                  {last
                    ? t("webhook.lastDelivery", { status: last.status, code: last.last_status_code ?? "—" })
                    : t("webhook.noDeliveries")}
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
