import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now } from "../http.ts";

/**
 * National Hurricane Center — active tropical cyclones.
 *
 * Complements `iem_asos`, which is observations only and therefore cannot say
 * what is FORECAST. Between them the weather domain now covers both what was
 * measured and what is currently expected.
 *
 * Tier A, for the advisory rather than for the forecast inside it. Issuing an
 * advisory is a formal act by a government agency with a timestamp and a number,
 * so "has NHC issued an advisory on storm X" and "what classification did NHC
 * assign" are settled here. The track and intensity FORECAST carried in that
 * advisory is a prediction and stays a prediction, which the note says.
 *
 * Verified 2026-09-08: one active storm (TS Marie, EP13). The feed is a single
 * unfiltered JSON document listing whatever is active right now, so this adapter
 * takes no query — there is nothing to query, and a params schema pretending
 * otherwise would invite calls that cannot be answered.
 */

const params = z.object({
  basin: z
    .enum(["atlantic", "pacific", "all"])
    .optional()
    .describe(
      "Filter by basin. Atlantic storm ids begin al, eastern Pacific ep, " +
        "central Pacific cp. Defaults to all.",
    ),
});

type Storm = {
  id?: string;
  binNumber?: string;
  name?: string;
  classification?: string;
  intensity?: string | number;
  pressure?: string | number;
  latitude?: string;
  longitude?: string;
  movementDir?: string | number;
  movementSpeed?: string | number;
  lastUpdate?: string;
  publicAdvisory?: { advNum?: string; issuance?: string; url?: string };
  forecastDiscussion?: { url?: string };
};

type Response = { activeStorms?: Storm[] };

const SOURCE = "https://www.nhc.noaa.gov/CurrentStorms.json";

/** NHC's two-letter classification codes, spelled out. */
const CLASSIFICATION: Record<string, string> = {
  TD: "Tropical Depression",
  TS: "Tropical Storm",
  HU: "Hurricane",
  PTC: "Potential Tropical Cyclone",
  STD: "Subtropical Depression",
  STS: "Subtropical Storm",
  EX: "Post-Tropical Cyclone",
  LO: "Low",
};

export const nhc = defineAdapter({
  id: "nhc_storms",
  name: "NHC Active Storms",
  tier: "A",
  domain: "weather",
  answers:
    "Tropical cyclones the US National Hurricane Center currently has " +
    "advisories out on: name, classification (depression, storm, hurricane), " +
    "maximum sustained winds, central pressure, position and movement. Covers " +
    "the Atlantic and the eastern and central Pacific. Resolution-grade for " +
    "'is there an active hurricane right now' and for what NHC has classified " +
    "a storm as. It is a SNAPSHOT of what is active — it holds no history, so " +
    "it cannot answer questions about past seasons or storms that have already " +
    "dissipated.",
  keywords: [
    "hurricane", "hurricanes", "storm", "storms", "cyclone", "tropical",
    "typhoon", "depression", "landfall", "nhc", "atlantic", "pacific",
    "basin", "winds", "advisory", "category", "season", "sustained",
  ],
  paramsSchema: params,
  paramsHelp:
    "Everything is optional — call it with no parameters for every active " +
    "storm. basin narrows to atlantic or pacific.",

  async run(input, signal) {
    const body = await getJson<Response>(SOURCE, {
      signal,
      context: "NHC",
      expect: (value) => isObject(value) && Array.isArray(value.activeStorms),
      expected: "an object with an activeStorms array",
    });

    let storms = body.activeStorms ?? [];
    const basin = input.basin ?? "all";
    if (basin !== "all") {
      const prefixes = basin === "atlantic" ? ["al"] : ["ep", "cp"];
      storms = storms.filter((storm) =>
        prefixes.some((prefix) => (storm.id ?? "").startsWith(prefix)),
      );
    }

    const { rows, truncated } = capRows(
      storms.map((storm) => ({
        name: storm.name,
        id: storm.id,
        classification: storm.classification,
        classificationName: storm.classification
          ? (CLASSIFICATION[storm.classification] ?? storm.classification)
          : undefined,
        maxWindsKt: storm.intensity,
        pressureMb: storm.pressure,
        position: `${storm.latitude ?? "?"} ${storm.longitude ?? "?"}`,
        movement:
          storm.movementDir !== undefined
            ? `${storm.movementDir}° at ${storm.movementSpeed} kt`
            : undefined,
        lastUpdate: storm.lastUpdate,
        advisoryNumber: storm.publicAdvisory?.advNum,
        advisoryIssued: storm.publicAdvisory?.issuance,
        url: storm.publicAdvisory?.url,
      })),
    );

    return {
      tier: "A" as const,
      url: SOURCE,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        (storms.length === 0
          ? "No active storms in this basin right now. That is a real answer " +
            "for THIS MOMENT and says nothing about the season as a whole. "
          : `${storms.length} active storm(s). `) +
        "Winds are maximum sustained in knots (multiply by 1.15 for mph); " +
        "hurricane strength begins at 64 kt. What is settled here is that NHC " +
        "has issued the advisory and assigned the classification — both formal " +
        "acts. Any track or intensity FORECAST inside that advisory remains a " +
        "forecast. This feed is a live snapshot with no history: for storms " +
        "that have already dissipated or for past seasons, it will be silent.",
    };
  },
});
