-- Read-only role for the chat agent. Run ONCE as the database owner.
-- The chat agent connects as this role so an LLM writing SQL can only ever
-- SELECT — it cannot write, drop, or reach other schemas. This is the safety
-- boundary; do not point chat.py at your owner/write credential.
--
--   psql "$DATABASE_URL" -f roles.sql      # as owner; edit the password first

create role caper_readonly with login password 'CHANGE_ME';

grant connect on database current_database() to caper_readonly;  -- (Supabase: replace with your db name)
grant usage on schema public to caper_readonly;
grant select on all tables in schema public to caper_readonly;

-- so future tables are readable too, without re-granting
alter default privileges in schema public grant select on tables to caper_readonly;

-- kill any runaway query the agent writes
alter role caper_readonly set statement_timeout = '15s';
