PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT    PRIMARY KEY,
    at_name         TEXT    NOT NULL,
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
    last_tweet_time TEXT DEFAULT NULL,
    deleted         TEXT DEFAULT NULL,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_at_name ON users(at_name);

CREATE TABLE IF NOT EXISTS follows (
    followee   TEXT    NOT NULL,
    follower   TEXT    NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT 1,
    checked_at TEXT,
    PRIMARY KEY (followee, follower)
);

CREATE TABLE IF NOT EXISTS blocks (
    blocker    TEXT NOT NULL,
    blocked    TEXT NOT NULL,
    updated_at TEXT DEFAULT NULL,
    PRIMARY KEY (blocker, blocked)
);

CREATE TABLE IF NOT EXISTS root_users (
    user_id              TEXT PRIMARY KEY REFERENCES users(user_id) DEFERRABLE INITIALLY DEFERRED,
    at_name              TEXT NOT NULL,
    followers_updated_at TEXT DEFAULT NULL,
    comment              TEXT DEFAULT NULL
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

CREATE VIEW IF NOT EXISTS user_follows AS
SELECT u0.user_id AS followee_id, u0.at_name AS followee, u1.at_name AS follower, u1.user_id AS follower_id
FROM follows
JOIN users AS u0 ON follows.followee=u0.user_id
JOIN users AS u1 ON follows.follower=u1.user_id;
