-- Supabase-only: row-level security and the storage bucket. Apply after 001.
--
-- The library is private. Row-level security grants reads to signed-in users
-- only, and the anon key -- the one a website ships -- reads nothing on its own.
-- For "only me", turn OFF new sign-ups in Authentication settings once your own
-- user exists; every authenticated user is then you.
--
-- Writes need no policy: the publisher and scoring jobs use the service-role /
-- secret key, which bypasses row-level security. That key must only ever exist
-- as a GitHub secret and in your local .env -- never in the website.

-- Explicit privileges, so this works with "Automatically expose new tables"
-- turned OFF (Supabase's recommendation). Two layers, both required to read:
-- the role needs a privilege on the table, and row-level security must let
-- the row through. anon gets neither, so the public key is refused outright.
revoke all on runs, forecasts, llm_calls, outcomes, scores from anon;
revoke all on forecast_library, accuracy_by_model, cost_by_week,
              questions_to_check, forecasts_to_score from anon;

grant select on runs, forecasts, llm_calls, outcomes, scores to authenticated;
grant select on forecast_library, accuracy_by_model, cost_by_week,
                questions_to_check, forecasts_to_score to authenticated;

-- The publisher and scoring job. The service role bypasses row-level
-- security but still needs table privileges once auto-expose is off.
grant select, insert, update, delete on runs, forecasts, llm_calls, outcomes, scores to service_role;
grant select on forecast_library, accuracy_by_model, cost_by_week,
                questions_to_check, forecasts_to_score to service_role;

alter table runs      enable row level security;
alter table forecasts enable row level security;
alter table llm_calls enable row level security;
alter table outcomes  enable row level security;
alter table scores    enable row level security;

drop policy if exists "signed-in read" on runs;
create policy "signed-in read" on runs      for select to authenticated using (true);
drop policy if exists "signed-in read" on forecasts;
create policy "signed-in read" on forecasts for select to authenticated using (true);
drop policy if exists "signed-in read" on llm_calls;
create policy "signed-in read" on llm_calls for select to authenticated using (true);
drop policy if exists "signed-in read" on outcomes;
create policy "signed-in read" on outcomes  for select to authenticated using (true);
drop policy if exists "signed-in read" on scores;
create policy "signed-in read" on scores    for select to authenticated using (true);

-- Private bucket for the per-question files. The site reads them through
-- short-lived signed URLs, which require this select policy.
insert into storage.buckets (id, name, public)
values ('forecast-runs', 'forecast-runs', false)
on conflict (id) do nothing;

drop policy if exists "signed-in read forecast files" on storage.objects;
create policy "signed-in read forecast files" on storage.objects
    for select to authenticated
    using (bucket_id = 'forecast-runs');
