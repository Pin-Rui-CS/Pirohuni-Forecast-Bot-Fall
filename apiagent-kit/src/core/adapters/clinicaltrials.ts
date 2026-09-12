import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * ClinicalTrials.gov API v2.
 *
 * v2, not the retired v1 — `api-registry-crossref.md` names v2 specifically.
 * Tier A: registration and status changes are recorded here as the primary act,
 * which is what makes "will trial X read out / complete / start phase 3"
 * resolvable rather than merely reportable.
 */

const params = z.object({
  condition: z
    .string()
    .optional()
    .describe('Disease or condition, e.g. "pancreatic cancer".'),
  intervention: z
    .string()
    .optional()
    .describe('Drug, device or procedure, e.g. "semaglutide".'),
  term: z
    .string()
    .optional()
    .describe("Free text across the whole record — sponsor names, trial titles."),
  status: z
    .enum([
      "NOT_YET_RECRUITING",
      "RECRUITING",
      "ACTIVE_NOT_RECRUITING",
      "COMPLETED",
      "TERMINATED",
      "WITHDRAWN",
      "SUSPENDED",
    ])
    .optional()
    .describe("Filter to one overall status."),
});

type Response = {
  totalCount?: number;
  studies?: Array<{
    protocolSection?: {
      identificationModule?: { nctId?: string; briefTitle?: string };
      statusModule?: {
        overallStatus?: string;
        startDateStruct?: { date?: string };
        primaryCompletionDateStruct?: { date?: string };
        completionDateStruct?: { date?: string };
        lastUpdateSubmitDate?: string;
      };
      designModule?: { phases?: string[] };
      sponsorCollaboratorsModule?: { leadSponsor?: { name?: string } };
    };
  }>;
};

export const clinicalTrials = defineAdapter({
  id: "clinicaltrials",
  name: "ClinicalTrials.gov",
  tier: "A",
  domain: "health",
  answers:
    "Registered clinical trials: status, phase, sponsor, start and completion " +
    "dates. Resolution-grade for 'will trial X complete / report / begin " +
    "phase N by DATE' and for counting trials matching a condition. Do NOT " +
    "use it for approval decisions (that is openFDA) or for trial RESULTS " +
    "published in journals.",
  keywords: [
    "trial", "trials", "clinical", "study", "phase", "recruiting", "drug",
    "therapy", "treatment", "cancer", "vaccine", "disease", "patients",
    "sponsor", "enrollment", "completion", "readout", "endpoint", "fda",
    "pharmaceutical", "medicine", "therapeutic", "condition",
  ],
  paramsSchema: params,
  paramsHelp:
    "Give at least one of condition, intervention or term. status narrows to a " +
    "single overall status.",

  async run(input, signal) {
    const url = new URL("https://clinicaltrials.gov/api/v2/studies");
    if (input.condition) url.searchParams.set("query.cond", input.condition);
    if (input.intervention) url.searchParams.set("query.intr", input.intervention);
    if (input.term) url.searchParams.set("query.term", input.term);
    if (input.status) url.searchParams.set("filter.overallStatus", input.status);
    url.searchParams.set("pageSize", "20");
    url.searchParams.set("countTotal", "true");
    url.searchParams.set("format", "json");

    const href = url.toString();
    const body = await getJson<Response>(href, {
      signal,
      context: "ClinicalTrials.gov",
      expect: (value) => isObject(value) && Array.isArray(value.studies),
      expected: "an object with a studies array",
    });

    const { rows, truncated } = capRows(
      (body.studies ?? []).map((study) => {
        const section = study.protocolSection;
        const status = section?.statusModule;
        return {
          nctId: section?.identificationModule?.nctId,
          title: truncate(section?.identificationModule?.briefTitle ?? "", 300),
          status: status?.overallStatus,
          phases: section?.designModule?.phases,
          sponsor: section?.sponsorCollaboratorsModule?.leadSponsor?.name,
          startDate: status?.startDateStruct?.date,
          primaryCompletion: status?.primaryCompletionDateStruct?.date,
          completion: status?.completionDateStruct?.date,
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
        "Completion dates are SPONSOR ESTIMATES until the status reads " +
        "COMPLETED. An estimated date is a plan, not an outcome." +
        (body.totalCount !== undefined && body.totalCount > rows.length
          ? ` ${body.totalCount} studies match in total.`
          : ""),
    };
  },
});
