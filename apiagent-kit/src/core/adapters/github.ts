import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, HttpError, isObject, now, truncate } from "../http.ts";

/**
 * GitHub — releases, tags and repository facts.
 *
 * Tier A for a narrow, well-defined thing: whether a release EXISTS and when it
 * was published. A release object is the act of shipping, timestamped by GitHub,
 * so "will project X ship v3 before DATE" is settled here rather than reported.
 *
 * The note draws the line explicitly, because the same response carries figures
 * that are NOT facts of that kind. Stars and forks measure popularity, and
 * popularity is an opinion poll with no methodology.
 *
 * Keyless at 60 requests/hour per IP, which is ample for one reader. Setting
 * `GITHUB_TOKEN` raises it to 5,000 and is read here if present — but the
 * adapter stays available either way, so there is no `available()` gate.
 */

const params = z.object({
  mode: z
    .enum(["releases", "repo", "search"])
    .describe(
      "releases = published releases newest first. repo = stars, language, " +
        "activity and the default branch. search = find a repository by name.",
    ),
  repo: z
    .string()
    .regex(/^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/)
    .optional()
    .describe('"owner/name", e.g. "vercel/next.js". Required for releases and repo.'),
  query: z.string().optional().describe('For mode=search, e.g. "vector database".'),
});

type Release = {
  tag_name?: string;
  name?: string;
  published_at?: string;
  created_at?: string;
  draft?: boolean;
  prerelease?: boolean;
  html_url?: string;
  body?: string;
};

type Repo = {
  full_name?: string;
  description?: string;
  stargazers_count?: number;
  forks_count?: number;
  open_issues_count?: number;
  language?: string;
  created_at?: string;
  pushed_at?: string;
  default_branch?: string;
  archived?: boolean;
  html_url?: string;
};

const API = "https://api.github.com";

function headers(): Record<string, string> {
  const token = process.env.GITHUB_TOKEN?.trim();
  return {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

export const github = defineAdapter({
  id: "github",
  name: "GitHub",
  tier: "A",
  domain: "technology",
  answers:
    "Software releases and repository facts: which versions have shipped and " +
    "when, plus stars, language, and when a project was last pushed to. " +
    "Resolution-grade for 'will PROJECT release version N by DATE' — a release " +
    "object is the shipping event itself, with GitHub's own timestamp. Do NOT " +
    "use it for software NOT hosted on GitHub, for download or usage numbers, " +
    "or for company product launches that are not a tagged release.",
  keywords: [
    "github", "release", "released", "version", "repo", "repository", "commit",
    "tag", "open", "source", "library", "framework", "package", "ship",
    "shipped", "stars", "fork", "maintainer", "changelog", "software", "code",
    "project", "developer", "npm", "python", "rust",
  ],
  paramsSchema: params,
  paramsHelp:
    'mode is required. releases and repo need repo as "owner/name"; search ' +
    "needs query. Use search first if you are unsure of the exact repo path.",

  async run(input, signal) {
    if (input.mode === "search") {
      if (!input.query) throw new Error("query is required when mode is search.");

      const url = new URL(`${API}/search/repositories`);
      url.searchParams.set("q", input.query);
      url.searchParams.set("sort", "stars");
      url.searchParams.set("per_page", "10");

      const href = url.toString();
      const body = await getJson<{ items?: Repo[]; total_count?: number }>(href, {
        signal,
        context: "GitHub search",
        headers: headers(),
        expect: (value) => isObject(value) && Array.isArray(value.items),
        expected: "an object with an items array",
      });

      const { rows, truncated } = capRows(
        (body.items ?? []).map((repo) => ({
          repo: repo.full_name,
          description: repo.description ? truncate(repo.description, 200) : undefined,
          stars: repo.stargazers_count,
          language: repo.language,
          lastPush: repo.pushed_at,
          url: repo.html_url,
        })),
      );

      return {
        tier: "A" as const,
        url: href,
        retrievedAt: now(),
        rows,
        truncated,
        note:
          "Repository paths only — call again with mode=releases for version " +
          "history. Ranked by stars, which is popularity and not relevance.",
      };
    }

    if (!input.repo) {
      throw new Error(`repo is required when mode is ${input.mode}.`);
    }

    if (input.mode === "repo") {
      const href = `${API}/repos/${input.repo}`;
      const repo = await getJson<Repo>(href, {
        signal,
        context: "GitHub repo",
        headers: headers(),
        expect: (value) => isObject(value) && "full_name" in value,
        expected: "a repository object",
      });

      return {
        tier: "A" as const,
        url: href,
        retrievedAt: now(),
        rows: [
          {
            repo: repo.full_name,
            description: repo.description,
            stars: repo.stargazers_count,
            forks: repo.forks_count,
            openIssues: repo.open_issues_count,
            language: repo.language,
            createdAt: repo.created_at,
            lastPush: repo.pushed_at,
            archived: repo.archived,
            url: repo.html_url,
          },
        ],
        note:
          "createdAt, lastPush and archived are facts about the repository. " +
          "Stars and forks are POPULARITY — a self-selected poll with no " +
          "methodology — so do not treat them as a measure of quality, " +
          "adoption or usage.",
      };
    }

    const url = new URL(`${API}/repos/${input.repo}/releases`);
    url.searchParams.set("per_page", "20");
    const href = url.toString();

    let releases: Release[];
    try {
      releases = await getJson<Release[]>(href, {
        signal,
        context: "GitHub releases",
        headers: headers(),
        expect: (value) => Array.isArray(value),
        expected: "an array of releases",
      });
    } catch (error) {
      // A missing or renamed repository is an answer, not a failure.
      if (error instanceof HttpError && error.status === 404) {
        return {
          tier: "A" as const,
          url: href,
          retrievedAt: now(),
          rows: [],
          note:
            `No repository at "${input.repo}". It may be private, renamed, or ` +
            "the path may be wrong — try mode=search.",
        };
      }
      throw error;
    }

    const { rows, truncated } = capRows(
      releases.map((release) => ({
        tag: release.tag_name,
        name: release.name || undefined,
        publishedAt: release.published_at,
        prerelease: release.prerelease,
        draft: release.draft,
        notes: release.body ? truncate(release.body, 400) : undefined,
        url: release.html_url,
      })),
    );

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${releases.length} releases, newest first. publishedAt is GitHub's own ` +
        "timestamp for the release, so it settles 'when did version X ship'. " +
        "Watch two things: a prerelease is not a stable release, and many " +
        "projects tag versions WITHOUT creating a release object, so an empty " +
        "list does not prove nothing shipped.",
    };
  },
});
