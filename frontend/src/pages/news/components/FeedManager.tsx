import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Input } from "../../../components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "../../../components/ui/card";
import { Badge } from "../../../components/ui/badge";
import { Skeleton } from "../../../components/ui/skeleton";
import { CheckCircle, XCircle, Rss, TestTube, Pencil, Trash2, Plus, Check, X } from "lucide-react";

type FeedHealth = {
  url: string;
  title?: string;
  status: "ok" | "error";
  items: number;
  error?: string | null;
};

type FeedsListResponse = {
  feeds: FeedHealth[];
  /** Where the list comes from: your list, the RSS_FEED_URLS env var, or the built-in defaults. */
  source?: "settings" | "env" | "default";
};

const SOURCE_TEXT: Record<string, string> = {
  settings: "Your list.",
  env: "From the server's RSS_FEED_URLS variable until you save a list here.",
  default: "The built-in defaults until you save a list here.",
};

type FeedTestResponse = {
  ok: boolean;
  feed_title: string;
  items: Array<{
    title: string;
    link: string;
    summary: string;
    published: string;
    source: string;
  }>;
  error?: string;
};

export function FeedManager() {
  const queryClient = useQueryClient();
  const [testUrl, setTestUrl] = useState("");
  const [newUrl, setNewUrl] = useState("");
  const [editing, setEditing] = useState<{ url: string; value: string } | null>(null);

  const feeds = useQuery({
    queryKey: ["news-feeds"],
    queryFn: () => api<FeedsListResponse>("/api/news/feeds"),
  });
  const urls = (feeds.data?.feeds ?? []).map((f) => f.url);

  const save = useMutation({
    mutationFn: (next: string[]) =>
      api<FeedsListResponse>("/api/news/feeds", { method: "PUT", body: JSON.stringify({ urls: next }) }),
    onSuccess: (data) => {
      queryClient.setQueryData(["news-feeds"], data);
      queryClient.invalidateQueries({ queryKey: ["news-rss"] });
      setNewUrl("");
      setEditing(null);
      toast.success("Feeds saved");
    },
    onError: (error) => toast.error("Could not save the feeds", {
      description: error instanceof Error ? error.message : "Unknown error",
    }),
  });

  const testFeed = useMutation({
    mutationFn: (url: string) =>
      api<FeedTestResponse>("/api/news/feeds/test", {
        method: "POST",
        body: JSON.stringify({ url }),
      }),
    onSuccess: (data) => {
      if (data.ok) {
        toast.success(`Feed test passed: ${data.feed_title || "Unnamed feed"} (${data.items.length} sample items)`);
      } else {
        toast.error(`Feed test failed: ${data.error || "Unknown error"}`);
      }
    },
    onError: (error) => {
      toast.error("Feed test failed", {
        description: error instanceof Error ? error.message : "Unknown error",
      });
    },
  });

  const add = () => {
    const url = newUrl.trim();
    if (url && !urls.includes(url)) save.mutate([...urls, url]);
  };

  return (
    <div className="space-y-4">
      <Card className="border-border bg-surface">
        <CardHeader>
          <CardTitle className="text-base text-text-primary flex items-center gap-2">
            <Rss className="h-4 w-4 text-accent" />
            Feeds
          </CardTitle>
          {feeds.data?.source ? <p className="text-xs text-text-muted">{SOURCE_TEXT[feeds.data.source]}</p> : null}
        </CardHeader>
        <CardContent className="space-y-3">
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              add();
            }}
          >
            <Input
              aria-label="New feed URL"
              placeholder="https://example.com/feed.xml"
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
              className="flex-1"
            />
            <Button type="submit" disabled={save.isPending || !newUrl.trim()}>
              <Plus className="mr-1 h-4 w-4" />
              Add
            </Button>
          </form>
          {feeds.isPending ? (
            <div className="space-y-2">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : feeds.isError ? (
            <div className="text-sm text-text-secondary">Failed to load the feeds</div>
          ) : (
            <ul className="space-y-2">
              {(feeds.data?.feeds ?? []).map((feed) => (
                <li key={feed.url} className="flex items-center gap-2 rounded-md border border-border p-3">
                  {editing?.url === feed.url ? (
                    <form
                      className="flex min-w-0 flex-1 gap-2"
                      onSubmit={(e) => {
                        e.preventDefault();
                        const value = editing.value.trim();
                        if (value) save.mutate(urls.map((u) => (u === feed.url ? value : u)));
                      }}
                    >
                      <Input
                        aria-label="Feed URL"
                        value={editing.value}
                        onChange={(e) => setEditing({ url: feed.url, value: e.target.value })}
                        className="flex-1"
                        autoFocus
                      />
                      <Button type="submit" size="icon" variant="ghost" aria-label="Save feed" disabled={save.isPending}>
                        <Check className="h-4 w-4" />
                      </Button>
                      <Button type="button" size="icon" variant="ghost" aria-label="Cancel" onClick={() => setEditing(null)}>
                        <X className="h-4 w-4" />
                      </Button>
                    </form>
                  ) : (
                    <>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-text-primary">{feed.title || feed.url}</p>
                        <p className="truncate text-xs text-text-secondary">
                          {feed.title ? `${feed.url} · ` : ""}
                          {feed.status === "ok" ? `${feed.items} items` : feed.error || "unreachable"}
                        </p>
                      </div>
                      <Badge
                        variant="outline"
                        className={feed.status === "ok" ? "border-success/50 text-success" : "border-danger/50 text-danger"}
                      >
                        {feed.status === "ok" ? "OK" : "Error"}
                      </Badge>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={`Edit ${feed.url}`}
                        onClick={() => setEditing({ url: feed.url, value: feed.url })}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={`Remove ${feed.url}`}
                        disabled={save.isPending}
                        onClick={() => save.mutate(urls.filter((u) => u !== feed.url))}
                      >
                        <Trash2 className="h-4 w-4 text-danger" />
                      </Button>
                    </>
                  )}
                </li>
              ))}
              {(feeds.data?.feeds ?? []).length === 0 && (
                <p className="text-sm text-text-secondary">No feeds configured</p>
              )}
            </ul>
          )}
          {feeds.data?.source === "settings" ? (
            <Button variant="outline" size="sm" disabled={save.isPending} onClick={() => save.mutate([])}>
              Restore the defaults
            </Button>
          ) : null}
        </CardContent>
      </Card>

      <Card className="border-border bg-surface">
        <CardHeader>
          <CardTitle className="text-base text-text-primary flex items-center gap-2">
            <TestTube className="h-4 w-4 text-accent" />
            Test a feed before adding it
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex gap-2">
            <Input
              aria-label="Feed URL to test"
              placeholder="https://example.com/feed.xml"
              value={testUrl}
              onChange={(e) => setTestUrl(e.target.value)}
              className="flex-1"
            />
            <Button
              onClick={() => testFeed.mutate(testUrl)}
              disabled={testFeed.isPending || !testUrl.trim()}
            >
              {testFeed.isPending ? "Testing..." : "Test"}
            </Button>
          </div>
          {testFeed.data && (
            <div className="rounded-md border border-border bg-surface-2 p-3 text-sm">
              {testFeed.data.ok ? (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 text-success">
                    <CheckCircle className="h-4 w-4" />
                    <span className="font-medium">Feed is reachable</span>
                  </div>
                  {testFeed.data.feed_title && (
                    <p className="text-text-secondary">Title: {testFeed.data.feed_title}</p>
                  )}
                  {testFeed.data.items.length > 0 && (
                    <div className="space-y-1">
                      <p className="text-text-secondary">Sample items:</p>
                      {testFeed.data.items.map((item, i) => (
                        <a
                          key={i}
                          href={item.link}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="block truncate text-accent hover:underline"
                        >
                          {item.title}
                        </a>
                      ))}
                    </div>
                  )}
                  {!urls.includes(testUrl.trim()) && (
                    <Button size="sm" variant="outline" disabled={save.isPending} onClick={() => save.mutate([...urls, testUrl.trim()])}>
                      <Plus className="mr-1 h-4 w-4" />
                      Add this feed
                    </Button>
                  )}
                </div>
              ) : (
                <div className="flex items-center gap-2 text-danger">
                  <XCircle className="h-4 w-4" />
                  <span>{testFeed.data.error || "Feed test failed"}</span>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
