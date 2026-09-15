-- Forecast library schema. Plain Postgres; Supabase-specific security lives in 002.
--
-- Re-runnable: every statement is idempotent, so applying the whole directory
-- again is safe. Add new migrations as new numbered files; do not edit applied
-- ones in ways that would change existing data.
--
-- Design: `forecasts.raw` holds each forecast.json verbatim and is the source of
-- truth for anything not promoted to a column. The promoted columns are filled
-- by eval_tools/publish_runs.py, which branches on schema_version, so a change
-- to forecast.json's shape is handled there and in the views below -- never by
-- rewriting stored rows.

create table if not exists runs (
    run_id          text primary key,           -- gh-<run id>-<attempt> or local-<ts>-<host>
    workflow        text,
    event           text,                       -- schedule, workflow_dispatch, local
    code_sha        text,
    run_url         text,
    run_timestamp   text,                       -- the docs/runs/<timestamp> folder name
    started_at      timestamptz,
    config          jsonb not null default '{}'::jsonb,  -- routing + CLI context
    published_at    timestamptz not null default now()
);

create table if not exists forecasts (
    run_id              text not null references runs (run_id) on delete cascade,
    question_id         bigint not null,
    post_id             bigint,
    question_type       text,
    title               text,
    run_at              timestamptz,
    workflow            text,
    submitted           boolean,
    abstained           boolean,
    total_cost_usd      numeric,
    estimated_tokens    bigint,
    tier1_model         text,
    tier2_model         text,
    code_sha            text,
    schema_version      integer not null default 1,
    run_url             text,
    blob_prefix         text not null,          -- storage path holding this question's files
    raw                 jsonb not null,         -- forecast.json, verbatim
    published_at        timestamptz not null default now(),
    primary key (run_id, question_id)
);

create index if not exists forecasts_question_idx on forecasts (question_id);
create index if not exists forecasts_run_at_idx on forecasts (run_at);

create table if not exists llm_calls (
    run_id                  text not null,
    question_id             bigint not null,
    call_no                 integer not null,
    task                    text,
    endpoint                text,
    cost_source             text,
    model                   text,
    input_tokens            bigint,
    output_tokens           bigint,
    native_input_tokens     bigint,
    native_output_tokens    bigint,
    reasoning_tokens        bigint,
    cached_input_tokens     bigint,
    cache_write_tokens      bigint,
    cost_usd                numeric,
    quota_microdollars      numeric,
    duration_seconds        numeric,
    primary key (run_id, question_id, call_no),
    foreign key (run_id, question_id) references forecasts (run_id, question_id) on delete cascade
);

-- One row per question. Written by eval_tools/score_outcomes.py.
create table if not exists outcomes (
    question_id     bigint primary key,
    post_id         bigint,
    status          text,                       -- open, closed, resolved, ...
    resolution      text,
    resolved_at     timestamptz,
    checked_at      timestamptz not null default now()
);

-- One row per forecast once its question resolves.
create table if not exists scores (
    run_id          text not null,
    question_id     bigint not null,
    metric          text not null,              -- brier (binary, multiple choice) or crps (numeric)
    score           double precision not null,  -- lower is better for both
    resolution      text,
    scored_at       timestamptz not null default now(),
    primary key (run_id, question_id),
    foreign key (run_id, question_id) references forecasts (run_id, question_id) on delete cascade
);

-- ---------------------------------------------------------------------------
-- Views. security_invoker makes them run with the CALLER's permissions, so the
-- row-level security in 002 applies through them. Without it a view runs as its
-- owner and would expose every row to anyone who can read the view.
-- ---------------------------------------------------------------------------

create or replace view forecast_library with (security_invoker = true) as
select
    f.run_id,
    f.question_id,
    f.post_id,
    f.title,
    f.question_type,
    f.run_at,
    f.workflow,
    f.submitted,
    f.abstained,
    f.tier1_model,
    f.tier2_model,
    f.code_sha,
    f.schema_version,
    f.total_cost_usd,
    case
        when f.question_type = 'binary' and jsonb_typeof(f.raw -> 'final_forecast') = 'number'
        then (f.raw ->> 'final_forecast')::double precision
    end                                         as probability_yes,
    o.status                                    as outcome_status,
    o.resolution,
    o.resolved_at,
    s.metric,
    s.score,
    f.blob_prefix,
    f.run_url
from forecasts f
left join outcomes o on o.question_id = f.question_id
left join scores s on s.run_id = f.run_id and s.question_id = f.question_id;

-- Accuracy of real submissions, by the models that produced them.
create or replace view accuracy_by_model with (security_invoker = true) as
select
    f.question_type,
    s.metric,
    f.tier1_model,
    f.tier2_model,
    count(*)            as n,
    avg(s.score)        as mean_score
from forecasts f
join scores s on s.run_id = f.run_id and s.question_id = f.question_id
where f.submitted
group by f.question_type, s.metric, f.tier1_model, f.tier2_model;

create or replace view cost_by_week with (security_invoker = true) as
select
    date_trunc('week', f.run_at)                as week,
    count(distinct (f.run_id, f.question_id))   as forecasts,
    sum(c.cost_usd)                             as cost_usd,
    sum(c.quota_microdollars)                   as quota_microdollars,
    sum(c.native_input_tokens)                  as input_tokens,
    sum(c.native_output_tokens)                 as output_tokens
from forecasts f
left join llm_calls c on c.run_id = f.run_id and c.question_id = f.question_id
group by 1;

-- Questions whose outcome is not yet known, stalest check first.
create or replace view questions_to_check with (security_invoker = true) as
select
    f.question_id,
    max(f.post_id)          as post_id,
    min(f.run_at)           as first_forecast_at,
    max(o.checked_at)       as last_checked_at
from forecasts f
left join outcomes o on o.question_id = f.question_id
where o.status is distinct from 'resolved'
group by f.question_id
order by max(o.checked_at) nulls first, min(f.run_at);

-- Forecasts on resolved questions that have no score yet. Covers forecasts
-- published after their question resolved, e.g. from a backfill.
create or replace view forecasts_to_score with (security_invoker = true) as
select f.run_id, f.question_id, o.resolution, f.raw
from forecasts f
join outcomes o on o.question_id = f.question_id and o.status = 'resolved'
left join scores s on s.run_id = f.run_id and s.question_id = f.question_id
where s.run_id is null;
