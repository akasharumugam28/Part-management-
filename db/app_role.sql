-- OPTIONAL: a least-privilege login role for the running app.
--
-- By default DATABASE_URL uses Supabase's "postgres" role, which can do far
-- more than this app needs. After schema_postgres.sql has been applied (and
-- the data migrated, which needs the owner role), you can create a role that
-- may only read and write rows in the app's tables:
--
--   1. Replace CHANGE-ME below with a long random password, e.g.
--        python -c "import secrets; print(secrets.token_urlsafe(32))"
--   2. Run this file in the Supabase SQL editor.
--   3. In Vercel, set DATABASE_URL to the transaction-pooler string with the
--      user "part_manager_app.<project-ref>" and this password.
--
-- Keep using the postgres role for `flask init-db`, `flask restore-db` and
-- the migration script, which create tables and reset sequences.

CREATE ROLE part_manager_app LOGIN PASSWORD 'CHANGE-ME' NOINHERIT;

GRANT USAGE ON SCHEMA public TO part_manager_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON admin, parts, sub_parts, rate_limits TO part_manager_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO part_manager_app;

-- The tables have RLS enabled with no policies; allow this role only.
CREATE POLICY app_all ON admin       TO part_manager_app USING (true) WITH CHECK (true);
CREATE POLICY app_all ON parts       TO part_manager_app USING (true) WITH CHECK (true);
CREATE POLICY app_all ON sub_parts   TO part_manager_app USING (true) WITH CHECK (true);
CREATE POLICY app_all ON rate_limits TO part_manager_app USING (true) WITH CHECK (true);

-- Stop runaway queries from holding a pooled connection.
ALTER ROLE part_manager_app SET statement_timeout = '15s';
