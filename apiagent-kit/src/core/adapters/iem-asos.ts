import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, csvToObjects, getText, now, ShapeError } from "../http.ts";

/**
 * Iowa Environmental Mesonet — global ASOS/METAR archive.
 *
 * `api-registry-crossref.md` §2a calls this "the single most consequential
 * difference between the two documents", and it is the only adapter here that
 * exists to fix a specific documented failure (Q44953).
 *
 * The argument, in short: the companion guide routed observed-weather questions
 * to `api.weather.gov`, which covers US jurisdictions ONLY — BIKF (Keflavík) is
 * not in it — and to Open-Meteo, which is reanalysis rather than the observed
 * METAR such questions actually turn on. IEM is a global METAR archive, needs no
 * key, takes a bare ICAO code, and returns the sky-cover columns directly.
 *
 * Tier A: a METAR is the observation of record.
 */

/*
 * MEASURED 2026-09-10, after this adapter returned a useless answer.
 *
 * Asked for the weather at JFK on 2026-09-07 it returned 25 rows — of which
 * exactly TWO carried a temperature — covering 00:00-01:51 UTC, which is the
 * evening of the 6th in New York. The answer could not be given and the model
 * correctly refused to guess. Three separate causes, all fixed below:
 *
 * 1. RESOLUTION. A station files one routine METAR per hour and an automated
 *    update every five minutes in between. The 5-minute rows leave tmpf, dwpf,
 *    relh and p01i as "M" — they are padding. A full day is 288 + 24 = 312 rows
 *    at ~271 bytes each: 78 KB, which is ten times MAX_RESULT_CHARS and three
 *    times the whole turn's budget. It could never have been returned. The 24
 *    routine reports are 6.5 KB and fit with room to spare, so `report_type=3`
 *    is now the default. The row cap was never the problem; the resolution was.
 *
 * 2. THE LOCAL DAY. `tz` was hardcoded to UTC, so "September 7" at a US station
 *    began at 20:00 local on the 6th. IEM will resolve the day boundary in any
 *    IANA zone, so `timezone` is now a parameter. It rejects an unknown zone
 *    outright rather than silently shifting, so a wrong value fails loudly.
 *
 * 3. UNFOLLOWABLE ADVICE. On truncating it said "narrow the date range", while
 *    the schema accepted whole dates only — there was nothing narrower to ask
 *    for. The model spent a step of its budget trying. IEM accepts hour1/hour2,
 *    so narrowing is now actually possible, and every truncation message below
 *    names something the caller can really do.
 */

const params = z.object({
  station: z
    .string()
    .regex(/^[A-Za-z0-9]{3,4}$/)
    .describe(
      'Bare ICAO airport code, e.g. "BIKF" for Keflavík, "KJFK" for JFK. ' +
        "No network prefix — IEM resolves the network itself.",
    ),
  startDate: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("First day to include, YYYY-MM-DD."),
  endDate: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Last day, YYYY-MM-DD. Same as startDate for a single day."),
  timezone: z
    .string()
    .optional()
    .describe(
      "IANA timezone deciding where the day starts and ends, e.g. " +
        '"America/New_York" for JFK, "Atlantic/Reykjavik" for BIKF. Pass the ' +
        "airport's own zone whenever the question is about a local calendar " +
        'day. Defaults to UTC, which for a US station means the requested ' +
        "day begins the previous evening local time.",
    ),
  interval: z
    .enum(["hourly", "all"])
    .optional()
    .describe(
      'Default "hourly": the routine reports a station files once an hour, ' +
        "which is what almost every question wants and is the only setting " +
        'that fits a whole day. Use "all" only for sub-hourly timing — when ' +
        "fog closed in, when a gust hit — and pair it with startHour/endHour.",
    ),
  startHour: z
    .number()
    .int()
    .min(0)
    .max(23)
    .optional()
    .describe("Restrict to this hour onward, in `timezone`. Needs endHour."),
  endHour: z
    .number()
    .int()
    .min(0)
    .max(23)
    .optional()
    .describe("Restrict to before this hour, in `timezone`. Needs startHour."),
});

/**
 * Requested explicitly rather than `data=all`.
 *
 * `all` returns about thirty columns, most of them irrelevant, and a day of
 * observations then costs more of the turn's character budget than the question
 * needs. skyc1/skyl1 are the cloud layers §2a names; metar is the raw coded
 * observation, which is what a careful reader will want to check.
 */
const FIELDS = [
  "tmpf",
  "dwpf",
  "relh",
  "drct",
  "sknt",
  "p01i",
  "vsby",
  "skyc1",
  "skyc2",
  "skyc3",
  "skyl1",
  "metar",
];

/** IEM's code for the routine hourly report. Verified against the live API. */
const ROUTINE_ONLY = "3";

export const iemAsos = defineAdapter({
  id: "iem_asos",
  name: "Iowa Environmental Mesonet (ASOS)",
  tier: "A",
  domain: "weather",
  answers:
    "OBSERVED surface weather at any airport worldwide: temperature, wind, " +
    "visibility, precipitation and cloud cover, from the METAR record. " +
    "Resolution-grade for 'was it clear/raining/above N degrees at PLACE on " +
    "DATE'. Works outside the United States, which api.weather.gov does not. " +
    "Do NOT use it for FORECASTS — this is what was measured, not what is " +
    "expected — and do NOT use it for city-wide or regional averages.",
  keywords: [
    "weather", "temperature", "rain", "snow", "wind", "cloud", "clouds",
    "cloudy", "clear", "sky", "visibility", "observed", "metar", "airport",
    "station", "precipitation", "humidity", "fog", "overcast", "eclipse",
    "hottest", "coldest", "degrees", "celsius", "fahrenheit", "conditions",
  ],
  paramsSchema: params,
  paramsHelp:
    "station, startDate and endDate are required; the rest have working " +
    "defaults. One day returns 24 hourly observations and fits comfortably — " +
    "ask for a single day at a time and you will never be truncated. Pass " +
    "timezone (the airport's IANA zone) whenever the question is about a " +
    'local calendar day. Reach for interval "all" only when you need timing ' +
    "finer than an hour, and give startHour/endHour with it.",

  async run(input, signal) {
    const [y1, m1, d1] = input.startDate.split("-");
    const [y2, m2, d2] = input.endDate.split("-");

    const timezone = input.timezone?.trim() || "UTC";
    const interval = input.interval ?? "hourly";

    const url = new URL("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py");
    url.searchParams.set("station", input.station.toUpperCase());
    for (const field of FIELDS) url.searchParams.append("data", field);
    url.searchParams.set("tz", timezone);
    url.searchParams.set("format", "onlycomma");
    url.searchParams.set("missing", "M");
    url.searchParams.set("trace", "T");
    url.searchParams.set("year1", y1);
    url.searchParams.set("month1", String(Number(m1)));
    url.searchParams.set("day1", String(Number(d1)));
    url.searchParams.set("year2", y2);
    url.searchParams.set("month2", String(Number(m2)));
    url.searchParams.set("day2", String(Number(d2)));

    if (interval === "hourly") url.searchParams.set("report_type", ROUTINE_ONLY);

    // Only meaningful together; one alone would silently widen the window back
    // to the whole day, which is the kind of quiet surprise this file exists to
    // stop. Both or neither.
    if (input.startHour !== undefined && input.endHour !== undefined) {
      url.searchParams.set("hour1", String(input.startHour));
      url.searchParams.set("minute1", "0");
      url.searchParams.set("hour2", String(input.endHour));
      url.searchParams.set("minute2", "0");
    }

    const href = url.toString();
    const text = await getText(href, { signal, context: "IEM ASOS" });

    /*
     * The exact §2a failure, guarded — and it still fires.
     *
     * Last time this endpoint was called with a lost parameter it returned the
     * Iowa landing page under a 200, and the pipeline recorded a successful
     * fetch of nothing. A valid CSV always begins with the station column, so
     * anything that does not is an error however healthy its status line looked.
     *
     * Observed 2026-09-10: this endpoint also answers "ERROR: server over
     * capacity, please try later" as a 200, and rejects a bad timezone with a
     * JSON validation error, also as a 200. Both land here, which is why the
     * body goes into the message — the model can act on "unknown timezone" and
     * cannot act on "request failed".
     */
    if (!text.startsWith("station,")) {
      throw new ShapeError(
        "IEM ASOS",
        "a CSV beginning with a station column",
        text.slice(0, 200),
      );
    }

    const observations = csvToObjects(text);

    /*
     * Summary over the WHOLE window, not just the rows that survive the cap.
     *
     * The convention `cboe-vix`, `pageviews` and `portwatch` already follow: a
     * truncated answer should still carry the shape of the full range, so the
     * model is not reduced to describing an arbitrary slice. Kept separate from
     * `rows` because these are DERIVED — the rows are the observation of
     * record, this is arithmetic over them, and the two should not be cited the
     * same way.
     */
    const temps = observations
      .map((row) => Number(row.tmpf))
      .filter((value) => Number.isFinite(value));
    const gusts = observations
      .map((row) => Number(row.sknt))
      .filter((value) => Number.isFinite(value));
    const wet = observations.filter((row) => {
      const rain = Number(row.p01i);
      return Number.isFinite(rain) ? rain > 0 : row.p01i === "T";
    }).length;

    // Newest first, matching every other time series in this registry, so a cap
    // keeps the end of the window nearest the question.
    const ordered = [...observations].reverse();
    const { rows, truncated } = capRows(ordered);

    /*
     * The period figures go in the note as prose, exactly as `cboe-vix` states
     * its period high and low.
     *
     * `AdapterResult` has no summary field, and the tool layer forwards a fixed
     * set of keys — so a structured one would be silently dropped in transit,
     * and adding it would mean a required change across all twenty-four
     * adapters for one adapter's benefit.
     */
    const period = temps.length
      ? `Over the whole window: high ${Math.max(...temps)}°F, low ` +
        `${Math.min(...temps)}°F` +
        (gusts.length ? `, peak wind ${Math.max(...gusts)} kt` : "") +
        (wet === 0
          ? ", no observation recorded precipitation"
          : `, ${wet} observation(s) recorded precipitation`) +
        ". Those are computed over every observation in range and hold even " +
        "if the rows below are truncated. "
      : "";

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${observations.length} observations, newest first, times in ` +
        `${timezone}` +
        (interval === "hourly"
          ? " (routine hourly reports). "
          : " (all reports, including 5-minute automated ones, which leave " +
            "temperature, dew point and humidity as M). ") +
        period +
        "M means missing, T means a trace. Sky cover uses METAR codes: " +
        "CLR/SKC clear, FEW, SCT scattered, BKN broken, OVC overcast; skyl1 " +
        "is the base height of the first layer in feet." +
        (truncated ? ` ${advice(interval, input)}` : ""),
    };
  },
});

/**
 * What to do about a truncated result — and only things that can actually be
 * done. The previous message told the caller to narrow a range that had no
 * narrower form, and a model duly spent a step discovering that.
 */
function advice(
  interval: "hourly" | "all",
  input: { startDate: string; endDate: string; startHour?: number },
): string {
  if (interval === "all" && input.startHour === undefined) {
    return (
      "Truncated: 5-minute reports are far more than one turn can carry. Use " +
      'interval "hourly" for whole-day questions, or give startHour and ' +
      "endHour to look at a few hours in detail."
    );
  }
  if (interval === "all") {
    return "Truncated — ask for a shorter startHour/endHour window.";
  }
  if (input.startDate !== input.endDate) {
    return "Truncated — ask for one day at a time; a single day always fits.";
  }
  return "Truncated — this station reports more often than once an hour.";
}
