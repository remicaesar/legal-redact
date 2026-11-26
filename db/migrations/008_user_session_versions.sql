-- Each user's session version. A session records the version it signed in
-- under and load_current_user refuses it once that no longer matches, so a
-- password change signs out every other session of that user.
--
-- A table beside users, not a column of it: an ALTER TABLE ADD COLUMN is not
-- idempotent, and a database built from db/schema.sql (which must already carry
-- the end state) would then fail this migration with a duplicate column. A user
-- with no row is at version 0, which is what every session signed in before
-- this table existed carries.

CREATE TABLE IF NOT EXISTS user_session_versions (
    user_id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
