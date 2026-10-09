-- Run diagnostics: one row per check per diagnosed forecast. Written by
-- eval_tools/diagnose_run.py --publish (manual "Diagnose a forecast" workflow).
-- Re-runnable, like 001/002. Apply after both.

create table if not exists run_diagnostics (
    run_id          text not null,
    question_id     bigint not null,
    check_id        text not null,              -- stable id, e.g. "pipeline.search_chain"
    category        text,                       -- pipeline | sources | condensation | evidence | forecast
    title           text,
    status          text not null,              -- pass | warn | fail | info | skipped
    detail          text,
    value           jsonb,                      -- machine-readable result of the check
    evidence        jsonb,                      -- list of supporting lines
    method          text,                       -- code | qwen
    diagnosed_at    timestamptz not null default now(),
    primary key (run_id, question_id, check_id),
    foreign key (run_id, question_id) references forecasts (run_id, question_id) on delete cascade
);

create index if not exists run_diagnostics_status_idx on run_diagnostics (status);

-- Pass/warn/fail counts per diagnosed forecast.
create or replace view diagnostics_summary with (security_invoker = true) as
select
    d.run_id,
    d.question_id,
    max(f.title)                                        as title,
    max(d.diagnosed_at)                                 as diagnosed_at,
    count(*) filter (where d.status = 'fail')           as fails,
    count(*) filter (where d.status = 'warn')           as warns,
    count(*) filter (where d.status = 'pass')           as passes,
    count(*) filter (where d.status = 'skipped')        as skipped
from run_diagnostics d
left join forecasts f on f.run_id = d.run_id and f.question_id = d.question_id
group by d.run_id, d.question_id;

-- Same access model as 002: signed-in read, service role writes, anon nothing.
revoke all on run_diagnostics from anon;
revoke all on diagnostics_summary from anon;
grant select on run_diagnostics to authenticated;
grant select on diagnostics_summary to authenticated;
grant select, insert, update, delete on run_diagnostics to service_role;
grant select on diagnostics_summary to service_role;

alter table run_diagnostics enable row level security;
drop policy if exists "signed-in read" on run_diagnostics;
create policy "signed-in read" on run_diagnostics for select to authenticated using (true);
