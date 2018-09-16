PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT    PRIMARY KEY,
    at_name         TEXT    NOT NULL UNIQUE,
    display_name    TEXT    NOT NULL,
    tweets          INTEGER NOT NULL,
    following       INTEGER NOT NULL,
    followers       INTEGER NOT NULL,
    likes           INTEGER NOT NULL,
    verified        BOOLEAN NOT NULL,
    protected       BOOLEAN NOT NULL,
    bio             TEXT,
    location        TEXT,
    url             TEXT,
    egg             BOOLEAN NOT NULL,
    created_at      TEXT    NOT NULL,
    lang            TEXT,
    time_zone       TEXT,
    utc_offset      INTEGER,
    last_tweet_time TEXT DEFAULT NULL,
    deleted         TEXT,
    updated_at      TEXT,
    UNIQUE (user_id, at_name)
);

CREATE TABLE IF NOT EXISTS follows (
    followee   TEXT    NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    follower   TEXT    NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    active     BOOLEAN NOT NULL DEFAULT 1,
    checked_at TEXT,
    PRIMARY KEY (followee, follower)
);

CREATE TABLE IF NOT EXISTS root_users (
    user_id              TEXT PRIMARY KEY,
    at_name              TEXT NOT NULL,
    followers_updated_at TEXT DEFAULT NULL,
    comment              TEXT DEFAULT NULL,
    FOREIGN KEY (user_id, at_name) REFERENCES users(user_id, at_name) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS whitelist (
    user_id TEXT PRIMARY KEY,
    at_name TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS deleted_users AS
SELECT *
FROM users
WHERE deleted NOT NULL
ORDER BY deleted DESC;
