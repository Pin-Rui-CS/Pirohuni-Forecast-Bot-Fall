import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * Launch Library 2 — orbital and suborbital launch schedule and history.
 *
 * `api-registry-crossref.md` §1 corrects the version to 2.3.0, which is the one
 * used here. Keyless, though the anonymous tier is rate limited.
 *
 * Tier A for launches that have already happened: the status field records the
 * outcome. Tier A is NOT claimed for scheduled dates, and the note below says
 * so — a launch date is a plan that slips constantly, and treating a scheduled
 * NET date as a fact is precisely the temporal-feasibility error §5 warns about.
 */

const params = z.object({
  mode: z
    .enum(["upcoming", "previous"])
    .describe("upcoming for scheduled launches, previous for ones that flew."),
  search: z
    .string()
    .optional()
    .describe('Filter by mission, rocket or provider, e.g. "Starship", "Ariane".'),
  since: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Earliest launch date, YYYY-MM-DD."),
  until: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Latest launch date, YYYY-MM-DD."),
});

type Launch = {
  id?: string;
  name?: string;
  net?: string;
  status?: { name?: string; abbrev?: string };
  launch_service_provider?: { name?: string };
  rocket?: { configuration?: { full_name?: string } };
  mission?: { name?: string; type?: string; orbit?: { name?: string } };
  pad?: { name?: string; location?: { name?: string } };
};

type Response = { count?: number; results?: Launch[] };

export const launchLibrary = defineAdapter({
  id: "launch_library",
  name: "Launch Library 2",
  tier: "A",
  domain: "science",
  answers:
    "Rocket launches: what is scheduled, what has already flown, the provider, " +
    "vehicle, mission, pad and outcome. Resolution-grade for 'did LAUNCH " +
    "succeed' and for counting launches in a past window. Treat SCHEDULED " +
    "dates as plans, not facts — they slip routinely. Do NOT use it for " +
    "satellite positions or for spacecraft already in orbit.",
  keywords: [
    "launch", "launches", "rocket", "spacex", "starship", "falcon", "nasa",
    "orbital", "space", "mission", "satellite", "spaceflight", "payload",
    "artemis", "ariane", "soyuz", "blue", "origin", "flight", "liftoff",
    "scrub", "pad", "orbit",
  ],
  paramsSchema: params,
  paramsHelp:
    "mode is required. search filters by name; since/until bound the launch " +
    "date. Use previous with a date range to count launches that happened.",

  async run(input, signal) {
    const url = new URL(`https://ll.thespacedevs.com/2.3.0/launches/${input.mode}/`);
    url.searchParams.set("limit", "20");
    // `normal`, not `list`: the list representation drops provider, rocket,
    // mission and pad, which are most of what a question actually asks about.
    // Measured — `list` returned name/net/status and nothing else.
    url.searchParams.set("mode", "normal");
    if (input.search) url.searchParams.set("search", input.search);
    if (input.since) url.searchParams.set("net__gte", `${input.since}T00:00:00Z`);
    if (input.until) url.searchParams.set("net__lte", `${input.until}T23:59:59Z`);
    // Newest first for history, soonest first for the schedule — in both cases
    // the end of the list nearest to now is the one being asked about.
    url.searchParams.set("ordering", input.mode === "previous" ? "-net" : "net");

    const href = url.toString();
    const body = await getJson<Response>(href, {
      signal,
      context: "Launch Library",
      expect: (value) => isObject(value) && Array.isArray(value.results),
      expected: "an object with a results array",
    });

    const { rows, truncated } = capRows(
      (body.results ?? []).map((launch) => ({
        name: truncate(launch.name ?? "", 200),
        net: launch.net,
        status: launch.status?.name,
        provider: launch.launch_service_provider?.name,
        rocket: launch.rocket?.configuration?.full_name,
        mission: launch.mission?.name,
        orbit: launch.mission?.orbit?.name,
        pad: launch.pad?.location?.name,
        url: launch.id
          ? `https://thespacedevs.com/llapi/launch/${launch.id}`
          : undefined,
      })),
    );

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${body.count ?? rows.length} launches match. "net" is No Earlier Than: ` +
        "for an upcoming launch it is a PLAN and slips routinely, so never " +
        "treat a scheduled date as settled. For previous launches the status " +
        "field records the actual outcome.",
    };
  },
});
