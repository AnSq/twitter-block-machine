CREATE TABLE IF NOT EXISTS users (
    twitter_id   TEXT    PRIMARY KEY,
    at_name      TEXT    NOT NULL,
    display_name TEXT    NOT NULL,
    tweets       INTEGER NOT NULL,
    following    INTEGER NOT NULL,
    followers    INTEGER NOT NULL,
    verified     INTEGER NOT NULL,
    protected    INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    bio          TEXT    DEFAULT '',
    deleted      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS causes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cause_type TEXT    NOT NULL,
    reason     TEXT    NOT NULL,
    UNIQUE (cause_type, reason) ON CONFLICT IGNORE
);

CREATE TABLE IF NOT EXISTS user_causes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT    REFERENCES users(twitter_id),
    cause         INTEGER REFERENCES causes(id),
    citation      TEXT             DEFAULT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    removed_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, cause) ON CONFLICT IGNORE
);

CREATE TABLE IF NOT EXISTS whitelist (
    user_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS deleted_types (
    id   INTEGER PRIMARY KEY,
    name TEXT    NOT NULL
);

CREATE VIEW IF NOT EXISTS removed_users AS
SELECT user_causes.user_id, users.at_name, users.display_name, users.tweets, users.following, users.followers, users.bio, user_causes.cause, user_causes.active, user_causes.removed_count, user_causes.active+user_causes.removed_count AS added_count
FROM users JOIN user_causes ON users.twitter_id==user_causes.user_id
WHERE removed_count > 0
ORDER BY added_count DESC, active DESC, removed_count DESC;