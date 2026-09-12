import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now } from "../http.ts";

/**
 * USGS earthquake catalogue (FDSN event service).
 *
 * Tier A. USGS is the authoritative source for magnitude, location and origin
 * time, and a question about whether a quake of some size occurred is settled
 * by its catalogue rather than reported by it.
 *
 * The one caveat worth carrying into an answer is that magnitudes are REVISED:
 * an event enters the catalogue automatically within minutes and is reviewed by
 * a seismologist afterwards, and the reviewed magnitude frequently differs from
 * the automatic one. The `status` field distinguishes them and the note says so
 * — this is the same vintage problem FRED has, in a different discipline.
 */

const params = z.object({
  from: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Earliest origin date, YYYY-MM-DD, UTC."),
  to: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Latest origin date, YYYY-MM-DD, UTC."),
  minMagnitude: z
    .number()
    .min(0)
    .max(10)
    .optional()
    .describe("Smallest magnitude to include. Defaults to 5."),
  place: z
    .string()
    .optional()
    .describe(
      'Case-insensitive filter on the place description, e.g. "Japan", ' +
        '"California". Applied locally, so it matches USGS wording like ' +
        '"37 km NE of Tambo, Peru".',
    ),
});

type Feature = {
  properties?: {
    mag?: number;
    place?: string;
    time?: number;
    updated?: number;
    url?: string;
    felt?: number | null;
    alert?: string | null;
    tsunami?: number;
    sig?: number;
    status?: string;
    magType?: string;
  };
  geometry?: { coordinates?: number[] };
};

type Response = { features?: Feature[]; metadata?: { count?: number } };

export const usgsEarthquakes = defineAdapter({
  id: "usgs_earthquakes",
  name: "USGS Earthquakes",
  tier: "A",
  domain: "geophysical",
  answers:
    "Earthquakes worldwide: magnitude, location, depth, origin time, whether a " +
    "tsunami warning was posted, and how widely the shaking was felt. " +
    "Resolution-grade for 'will there be a magnitude N+ quake in PLACE before " +
    "DATE' and for counting events in a window. Do NOT use it for volcanic " +
    "eruptions, landslides, or damage and casualty figures — none of those are " +
    "in this catalogue.",
  keywords: [
    "earthquake", "earthquakes", "quake", "seismic", "magnitude", "tremor",
    "aftershock", "richter", "epicenter", "epicentre", "fault", "tsunami",
    "shaking", "usgs", "seismology", "geological", "richter",
  ],
  paramsSchema: params,
  paramsHelp:
    "from and to are required. minMagnitude defaults to 5 — lower it for a " +
    "specific region, raise it for global questions or you will get thousands.",

  async run(input, signal) {
    const url = new URL("https://earthquake.usgs.gov/fdsnws/event/1/query");
    url.searchParams.set("format", "geojson");
    url.searchParams.set("starttime", input.from);
    // The FDSN endtime is exclusive of times after it, so name the end of the
    // day rather than midnight or the final day is silently dropped.
    url.searchParams.set("endtime", `${input.to}T23:59:59`);
    url.searchParams.set("minmagnitude", String(input.minMagnitude ?? 5));
    url.searchParams.set("orderby", "magnitude");
    url.searchParams.set("limit", "200");

    const href = url.toString();
    const body = await getJson<Response>(href, {
      signal,
      context: "USGS",
      expect: (value) => isObject(value) && Array.isArray(value.features),
      expected: "a GeoJSON FeatureCollection with a features array",
    });

    let events = body.features ?? [];
    if (input.place) {
      const needle = input.place.toLowerCase();
      events = events.filter((feature) =>
        (feature.properties?.place ?? "").toLowerCase().includes(needle),
      );
    }

    const matched = events.length;
    const { rows, truncated } = capRows(
      events.map((feature) => {
        const p = feature.properties ?? {};
        const [lon, lat, depth] = feature.geometry?.coordinates ?? [];
        return {
          magnitude: p.mag,
          magType: p.magType,
          place: p.place,
          time: p.time ? new Date(p.time).toISOString() : undefined,
          depthKm: depth,
          latitude: lat,
          longitude: lon,
          tsunami: p.tsunami === 1,
          alert: p.alert ?? undefined,
          feltReports: p.felt ?? undefined,
          status: p.status,
          url: p.url,
        };
      }),
    );

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${matched} events matched, largest first. ` +
        "status 'reviewed' means a seismologist has confirmed it; 'automatic' " +
        "means the magnitude may still be revised, and revisions of a few " +
        "tenths are routine. A question phrased at a threshold (\"magnitude 7 " +
        "or above\") can flip on that revision, so quote the status. " +
        "tsunami=true means a warning was POSTED, not that a wave arrived.",
    };
  },
});
