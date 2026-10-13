-- Run lineage: one row per diagnosed forecast holding its flow map, API calls
-- and evidence lifecycle (lineage.json; spec in diagnostics/LINEAGE.md).
-- Written by eval_tools/diagnose_run.py --publish. The step files it points to
-- live in storage at <forecasts.blob_prefix>/steps/<file>.gz.
-- Re-runnable, like 001-003. Apply after them.

create table if not exists run_lineage (
    run_id          text not null,
    question_id     bigint not null,
    schema_version  int not null,
    lineage_source  text,                       -- declared | legacy-translation
    lineage         jsonb not null,
    diagnosed_at    timestamptz not null default now(),
    primary key (run_id, question_id),
    foreign key (run_id, question_id) references forecasts (run_id, question_id) on delete cascade
);

-- Same access model as 002/003: signed-in read, service role writes, anon nothing.
revoke all on run_lineage from anon;
grant select on run_lineage to authenticated;
grant select, insert, update, delete on run_lineage to service_role;

alter table run_lineage enable row level security;
drop policy if exists "signed-in read" on run_lineage;
create policy "signed-in read" on run_lineage for select to authenticated using (true);
