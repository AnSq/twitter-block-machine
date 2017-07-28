#!/usr/bin/env python

import os
import sys
import json
import time
import twitter
import requests_oauthlib
import sqlite3
import urllib3
import multiprocessing
import traceback

from pprint import pprint as pp


CONSUMER_FILE = "consumer.json"
RLE = "Rate limit exceeded"


urllib3.disable_warnings()



class DatabaseAccess (object):
    """Provides access to the database"""

    def __init__(self, fname="database.sqlite"):
        """Connects to the database, creating it if needed"""

        self.conn = sqlite3.connect(fname)
        self.cur = self.conn.cursor()

        self.cur.executescript("""
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
                bio          TEXT    DEFAULT ''
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
        """)
        self.commit()


    def commit(self):
        """Commits pending changes to the database"""
        self.conn.commit()


    def rollback(self):
        """Reverts pending changes to the database"""
        self.conn.rollback()


    def add_cause(self, cause_type, reason):
        """Adds a cause to the list of available causes. Does nothing if it already exists"""
        self.cur.execute("INSERT OR IGNORE INTO causes (cause_type, reason) VALUES (?,?);", [cause_type, reason])


    def get_cause(self, cause_type, reason):
        """Gets the ID of the given cause"""
        rs = self.cur.execute("SELECT id FROM causes WHERE cause_type == ? AND reason == ?", [cause_type, reason])
        row = rs.fetchone()
        return row[0] if row else None


    def add_user(self, user):
        """Adds a user to the database, or updates them if they're already in it"""

        data = {
            "twitter_id"   : str(user.id),
            "at_name"      : user.screen_name,
            "display_name" : user.name,
            "tweets"       : user.statuses_count,
            "following"    : user.friends_count,
            "followers"    : user.followers_count,
            "verified"     : user.verified,
            "protected"    : user.protected,
            "created_at"   : created_at(user.created_at),
            "bio"          : user.description
        }

        self.cur.execute("""
            UPDATE OR IGNORE users SET
            at_name=:at_name, display_name=:display_name, tweets=:tweets, following=:following, followers=:followers, verified=:verified, protected=:protected, created_at=:created_at, bio=:bio
            WHERE twitter_id=:twitter_id;
            """,
            data
        )

        self.cur.execute ("""
            INSERT OR IGNORE INTO users
            (        twitter_id,  at_name,  display_name,  tweets,  following,  followers,  verified,  protected,  created_at,  bio)
            VALUES (:twitter_id, :at_name, :display_name, :tweets, :following, :followers, :verified, :protected, :created_at, :bio);
            """,
            data
        )


    def add_user_cause(self, user_id, cause_id, citation=None):
        """Adds a user-cause relationship to the database.
        Activates the relationship if it already exists.
        Does nothing if it's already active."""

        data = {
            "user_id":  str(user_id),
            "cause":    cause_id,
            "citation": citation
        }
        self.cur.execute("UPDATE OR IGNORE user_causes SET citation=:citation, active=1 WHERE user_id==:user_id AND cause==:cause;",    data)
        self.cur.execute("INSERT OR IGNORE INTO user_causes (user_id, cause, citation) VALUES (:user_id, :cause, :citation);", data)


    def get_user_ids_by_cause(self, cause_id):
        """Gets all user IDs that are active with the given cause. (Returns a generator)"""

        rs = self.cur.execute("SELECT user_id FROM user_causes WHERE cause==? AND active==?;", [cause_id, 1])
        while True:
            chunk = rs.fetchmany(256)
            if not chunk:
                break
            for r in chunk:
                yield int(r[0])


    def deactivate_user_cause(self, twitter_id, cause_id):
        """Deactivates a user-cause relationship.
        (Deactivated user-cause relationships represent users that previously
        matched a cause but don't anymore. For example, if they unfollowed the
        root user of a "follows" type cause. User-cause relationships can be
        reactivated if, for example, a user re-follows someone.)"""

        self.cur.execute("UPDATE user_causes SET active=0, removed_count=removed_count+1 WHERE user_id==? AND cause==?;", [str(twitter_id), cause_id])


    def is_user_cause_active(self, twitter_id, cause_id):
        """Checks if a user-cause relationship is active"""

        rs = self.cur.execute("SELECT user_id FROM user_causes WHERE user_id==? AND cause==? AND active==1;", [str(twitter_id), cause_id])
        return bool(rs.fetchone())


    def get_atname_by_id(self, twitter_id):
        """gets the at_name associated with a Twitter ID"""

        rs = self.cur.execute("SELECT at_name FROM users WHERE twitter_id==?;", [str(twitter_id)])

        result = rs.fetchone()
        if result:
            return result[0]
        else:
            return None



class FollowerBlocker (object):
    """Blocks the followers of one specific user (called the root user)"""

    def __init__(self, api, db, root_at_name):
        """Initialize the object with root_at_name as the root user's twitter handle"""
        self.api = api
        self.db = db
        self.root_at_name = root_at_name

        self.db.add_cause("follows", self.root_at_name)
        self.db.commit()
        self.cause_id = self.db.get_cause("follows", self.root_at_name)


    def _get_follower_ids_ratelimited(self):
        """Handles ratelimiting when getting follower IDs"""

        cursor = -1
        results = []

        i = 0
        while True:
            limit_block(self.api, "/followers/ids", i)

            try:
                cursor, prev_cursor, chunk = self.api.GetFollowerIDsPaged(screen_name=self.root_at_name, cursor=cursor)
            except twitter.error.TwitterError as e:
                if e.message[0]["message"] == RLE: pass
                else: raise
            results += chunk

            i += 1

            if cursor == 0:
                break

        return results


    def get_followers(self):
        """Gets the followers of the object's root user"""

        root_user = self.api.GetUser(screen_name=self.root_at_name, include_entities=False)
        print "Getting %d followers of @%s" % (root_user.followers_count, root_user.screen_name)

        print "Getting follower IDs"
        follower_ids = self._get_follower_ids_ratelimited()

        chunks = [follower_ids[i:i+100] for i in xrange(0, len(follower_ids), 100)]

        pool = multiprocessing.Pool(32)
        followers = []
        results = []

        start = time.time()

        print "Getting follower objects"
        try:
            for i,chunk in enumerate(chunks):
                results.append(pool.apply_async(get_followers_chunck, (self.api, chunk, i, len(chunks))))
            pool.close()
            for r in results:
                r.wait(999999999)
        except KeyboardInterrupt:
            pool.terminate()
            print
            sys.exit()
        else:
            pool.join()

        for r in results:
            try:
                followers += r.get()
            except:
                print "???"
                raise

        print "\nget_followers() done in %f" % (time.time() - start)

        return followers


    def scan(self):
        """Does one pass of getting the root user's followers, updating the
        database, and blocking/unblocking as necessary"""

        followers = self.get_followers()
        follower_ids = set(u.id for u in followers)
        old_followers = self.db.get_user_ids_by_cause(self.cause_id)

        blocks = 0
        unblocks = 0

        # deactivate users that no longer follow the root user
        for uid in old_followers:
            if uid not in follower_ids:
                self.db.deactivate_user_cause(uid, self.cause_id)
                self.unblock(uid)
                unblocks += 1

        for follower in followers:
            if not self.db.is_user_cause_active(follower.id, self.cause_id):
                self.block(follower)
                blocks += 1
            self.db.add_user(follower)
            self.db.add_user_cause(follower.id, self.cause_id)

        self.db.commit()

        print "%d blocks, %d unblocks" % (blocks, unblocks)


    def block(self, user):
        """Blocks the user represented by the specified user object"""
        print "Block @%s" % user.screen_name


    def unblock(self, twitter_id):
        """Unblocks the user represented by the specified user ID"""
        print "Unblock @%s (%d)" % (self.db.get_atname_by_id(twitter_id), twitter_id)



def login(username, consumer_file=CONSUMER_FILE, sleep=True):
    """Login to Twitter with the specified username.
    consumer_file is the name of a JSON file with the app's consumer key and secret.
    sleep is passed to the API wrapper as sleep_on_rate_limit."""

    print "Logging in @%s" % username

    with open(consumer_file) as f:
        consumer_token = json.load(f)
    consumer_key    = consumer_token["consumer_key"]
    consumer_secret = consumer_token["consumer_secret"]

    user_fname = username.lower() + ".token.json"

    if os.path.isfile(user_fname):
        with open(user_fname) as f:
            user_token = json.load(f)
        access_token        = user_token["access_token"]
        access_token_secret = user_token["access_token_secret"]
    else:
        oauth = requests_oauthlib.OAuth1Session(consumer_key, client_secret=consumer_secret, callback_uri='oob')

        req_token = oauth.fetch_request_token("https://api.twitter.com/oauth/request_token")
        auth_url  = oauth.authorization_url("https://api.twitter.com/oauth/authorize")

        print "\nGo to this URL to get a PIN code (make sure you're logged in as @%s):" % username
        print "\t%s\n" % auth_url

        pin = raw_input("Enter your PIN: ")

        oauth = requests_oauthlib.OAuth1Session(
            consumer_key,
            client_secret=consumer_secret,
            resource_owner_key=req_token.get('oauth_token'),
            resource_owner_secret=req_token.get('oauth_token_secret'),
            verifier=pin
        )

        acc_token = oauth.fetch_access_token("https://api.twitter.com/oauth/access_token")

        access_token        = acc_token.get('oauth_token')
        access_token_secret = acc_token.get('oauth_token_secret')

    api = twitter.Api(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        access_token_key=access_token,
        access_token_secret=access_token_secret,
        sleep_on_rate_limit=sleep
    )

    user = api.VerifyCredentials(skip_status=True)
    if user.screen_name.lower() != username.lower():
        print "\nLogged in user is @%s, not @%s. Exiting." % (user.screen_name, username)
        sys.exit()
    else:
        print "\nLogged in successfully as @%s" % user.screen_name
        if not os.path.exists(user_fname):
            user_token = {
                "access_token":         access_token,
                "access_token_secret" : access_token_secret
            }
            with open(user_fname, "w") as f:
                json.dump(user_token, f)
            print "User token saved to %s" % user_fname
        print

    return api


def created_at(created_at):
    """Converts a timestamp string to one more suited for sorting"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(created_at, "%a %b %d %H:%M:%S +0000 %Y"))


def limit_block(api, endpoint, i=-1):
    """Attempts to stop us from exceeding our API rate limit for the given endpoint.
    The API object doesn't always have complete information on our remaining
    limits (especially when multithreading), so it doesn't always work."""

    limit = api.rate_limit.get_limit(endpoint)
    if limit.remaining == 0:
        sleep_for = limit.reset - time.time() + 5
        print "[i=%d] Rate limit reached for %s. Sleeping for %.0f seconds (%.1f minutes. Until %s)" % (i, endpoint, sleep_for, sleep_for/60, time.strftime("%H:%M:%S %p", time.localtime(limit.reset+5)))
        time.sleep(sleep_for)
        print "Resuming"


def get_followers_chunck(api, chunk, i, num_chunks):
    """Threadable function for looking up user objects. Used by FollowerBlocker.get_followers()"""

    sys.stdout.write("\r\x1b[K") #carriage return and clear line
    sys.stdout.write("chunk %d/%d - %.2f%%" % (i+1, num_chunks, float(i+1)*100/num_chunks))
    sys.stdout.flush()

    while True:
        limit_block(api, "/users/lookup", i)
        try:
            return api.UsersLookup(user_id=chunk, include_entities=False)
        except twitter.error.TwitterError as e:
            e_type, e_val, e_trace = sys.exc_info()
            try:
                if e.message[0]["message"] == RLE: pass
                else:
                    print e.message
                    raise
            except:
                print "=== THREAD %4d ==========" % i
                traceback.print_exc()
                print "==========================="

                with open("error_logs/%s.log" % multiprocessing.current_process().name, "w") as f:
                    traceback.print_exc(file=f)

                raise e_type, e_val, e_trace



def main():
    db = DatabaseAccess("test.sqlite")
    api = login(sys.argv[1])
    print api.VerifyCredentials(skip_status=True)
    print

    fb = FollowerBlocker(api, db, "RichardBSpencer")
    fb.scan()


if __name__ == "__main__":
    main()
