#!/usr/bin/env python

import os
import sys
import json
import time
import math
import twitter
import requests_oauthlib
import sqlite3
import urllib3
import multiprocessing
import traceback

from pprint import pprint as pp


CONSUMER_FILE = "consumer.json"
RLE = "Rate limit exceeded"
DELETED = 1
SUSPENDED = 2


urllib3.disable_warnings()



class DatabaseAccess (object):
    """Provides access to the database"""

    def __init__(self, fname="database.sqlite"):
        """Connect to the database, creating it if needed"""

        self.fname = fname
        self.conn = sqlite3.connect(self.fname)
        self.cur = self.conn.cursor()

        with open("schema.sql") as f:
            self.cur.executescript(f.read())
        self.cur.execute("PRAGMA foreign_keys = ON;")
        self.cur.execute("INSERT OR IGNORE INTO deleted_types (id, name) VALUES (?, 'deleted'), (?, 'suspended');", [DELETED, SUSPENDED])
        self.commit()


    def __repr__(self):
        return '<%s.%s(fname="%s")>' % (self.__module__, self.__class__.__name__, self.fname)


    @staticmethod
    def _replace_url_entities(user, attr):
        """replace Twitter URL entities in the given attribute of the given user and returns the result"""
        s = user.__getattribute__(attr)
        try:
            for en in user._json["entities"][attr]["urls"]:
                s = s.replace(en["url"], en["expanded_url"])
        except KeyError as e:
            pass
        finally:
            return s.replace("http://", "").replace("https://", "") if s else s


    def commit(self):
        """Commit pending changes to the database"""
        self.conn.commit()


    def rollback(self):
        """Revert pending changes to the database"""
        self.conn.rollback()


    def add_cause(self, cause_type, reason):
        """Add a cause to the list of available causes. Does nothing if it already exists"""
        try:
            self.cur.execute("INSERT OR FAIL INTO causes (cause_type, reason) VALUES (?,?);", [cause_type, reason])
        except sqlite3.IntegrityError as e:
            pass


    def add_whitelist(self, user_id):
        """Add a user to the whitelist. DOES NOT unblock them. Blockers are responsible for processing the whitelist."""
        self.cur.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?);", [user_id])


    def get_cause(self, cause_type, reason):
        """Get the ID of the given cause"""
        rs = self.cur.execute("SELECT id FROM causes WHERE cause_type==? AND reason==?", [cause_type, reason])
        row = rs.fetchone()
        return row[0] if row else None


    def add_user(self, user):
        """Add a user to the database, or updates them if they're already in it"""

        data = {
            "twitter_id"   : str(user.id),
            "at_name"      : user.screen_name,
            "display_name" : user.name,
            "tweets"       : user.statuses_count,
            "following"    : user.friends_count,
            "followers"    : user.followers_count,
            "likes"        : user.favourites_count,
            "verified"     : user.verified,
            "protected"    : user.protected,
            "bio"          : self._replace_url_entities(user, "description") or None,
            "location"     : user.location or None,
            "url"          : self._replace_url_entities(user, "url"),
            "egg"          : not user.default_profile_image,
            "created_at"   : created_at(user.created_at),
            "lang"         : user.lang or None,
            "time_zone"    : user.time_zone,
            "utc_offset"   : user.utc_offset,
            "deleted"      : 0
        }

        self.cur.execute("""
            UPDATE OR IGNORE users SET
            at_name=:at_name, display_name=:display_name, tweets=:tweets, following=:following, followers=:followers, likes=:likes, verified=:verified, protected=:protected, bio=:bio, location=:location, url=:url, egg=:egg, created_at=:created_at, lang=:lang, time_zone=:time_zone, utc_offset=:utc_offset, deleted=:deleted
            WHERE twitter_id=:twitter_id;
            """,
            data
        )

        self.cur.execute ("""
            INSERT OR IGNORE INTO users
            (        twitter_id,  at_name,  display_name,  tweets,  following,  followers,  likes,  verified,  protected,  bio,  location,  url,  egg,  created_at,  lang,  time_zone,  utc_offset,  deleted)
            VALUES (:twitter_id, :at_name, :display_name, :tweets, :following, :followers, :likes, :verified, :protected, :bio, :location, :url, :egg, :created_at, :lang, :time_zone, :utc_offset, :deleted);
            """,
            data
        )


    def add_user_cause(self, user_id, cause_id, citation=None):
        """Add a user-cause relationship to the database.
        Activates the relationship if it already exists.
        Does nothing if it's already active."""
        data = {
            "user_id":  str(user_id),
            "cause":    cause_id,
            "citation": citation
        }
        self.cur.execute("UPDATE OR IGNORE user_causes SET citation=:citation, active=1 WHERE user_id==:user_id AND cause==:cause;", data)

        try:
            self.cur.execute("INSERT OR FAIL INTO user_causes (user_id, cause, citation) VALUES (:user_id, :cause, :citation);", data)
        except sqlite3.IntegrityError as e:
            pass


    def get_user_ids_by_cause(self, cause_id):
        """Get all user IDs that are active with the given cause. (Returns a generator)"""
        rs = self.cur.execute("SELECT user_id FROM user_causes WHERE cause==? AND active==?;", [cause_id, 1])
        while True:
            chunk = rs.fetchmany(256)
            if not chunk:
                break
            for r in chunk:
                yield int(r[0])


    def deactivate_user_cause(self, twitter_id, cause_id):
        """Deactivate a user-cause relationship.
        (Deactivated user-cause relationships represent users that previously
        matched a cause but don't anymore. For example, if they unfollowed the
        root user of a "follows" type cause. User-cause relationships can be
        reactivated if, for example, a user re-follows someone.)"""
        self.cur.execute("UPDATE user_causes SET active=0, removed_count=removed_count+1 WHERE user_id==? AND cause==?;", [str(twitter_id), cause_id])


    def whitelist_user_cause(self, twitter_id, cause_id):
        """Deactivate a user-cause relationship due to whitelisting.
        Does not increment removed_count."""
        self.cur.execute("UPDATE user_causes SET active=0 WHERE user_id==? AND cause==?;", [str(twitter_id), cause_id])


    def is_user_cause_active(self, twitter_id, cause_id):
        """Check if a user-cause relationship is active"""
        rs = self.cur.execute("SELECT user_id FROM user_causes WHERE user_id==? AND cause==? AND active==1;", [str(twitter_id), cause_id])
        return bool(rs.fetchone())


    def get_atname_by_id(self, twitter_id):
        """get the at_name associated with a Twitter ID"""
        rs = self.cur.execute("SELECT at_name FROM users WHERE twitter_id==?;", [str(twitter_id)])
        result = rs.fetchone()
        if result:
            return result[0]
        else:
            return None


    def get_active_whitelisted(self, cause_id):
        """Get user IDs that are active with the given cause and whitelisted."""
        rs = self.cur.execute("""
            SELECT twitter_id FROM users JOIN user_causes ON twitter_id==user_id
            WHERE cause==? AND active==1 AND twitter_id IN whitelist;
            """,
            [cause_id]
        )
        return [int(x[0]) for x in rs.fetchall()]


    def get_whitelist(self):
        """return the entire whitelist as a list"""
        rs = self.cur.execute("SELECT user_id FROM whitelist;")
        return [int(x[0]) for x in rs.fetchall()]


    def set_user_deleted(self, twitter_id, deleted_status):
        """mark the given user as deleted or suspended"""
        self.cur.execute("UPDATE users SET deleted=? WHERE twitter_id==?;", [deleted_status, str(twitter_id)])



class Blocker (object):
    """Base object for blockers"""

    def __init__(self, api, db, live=False):
        self.api = api
        self.db = db
        self.live = live
        self.load_whitelist()
        self.username = api.VerifyCredentials().screen_name


    def __repr__(self):
        return '<%s.%s [user="%s", db="%s"]>' % (self.__module__, self.__class__.__name__, self.username, self.db.fname)


    def _get_ids_paged_ratelimited(self, num_pages, page_function, page_kwargs, endpoint):
        """Get paginated IDs, handling ratelimiting"""
        cursor = -1
        results = []

        i = 0
        while True:
            limit_block(self.api, endpoint, i)

            cr()
            sys.stdout.write("page %d/%d" % (i+1, num_pages))
            sys.stdout.flush()

            try:
                cursor, prev_cursor, chunk = page_function(cursor=cursor, **page_kwargs)
            except twitter.error.TwitterError as e:
                if error_message(e, RLE): continue
                else: raise
            results += chunk

            i += 1

            if cursor == 0:
                break

        return results


    def _do_block(self, users_to_block):
        """Block users, multithreaded"""
        print "Blocking %d users..." % len(users_to_block)
        pool = multiprocessing.Pool(min(32, len(users_to_block)))
        results = []
        start = time.time()
        try:
            for i,user in enumerate(users_to_block):
                results.append(pool.apply_async(block_wrapper, [self, user, i, len(users_to_block)]))
            pool.close()
            for r in results:
                r.wait(999999999)
        except KeyboardInterrupt:
            pool.terminate()
            print
            sys.exit()
        else:
            pool.join()
        print "Blocking completed in %.2f seconds" % (time.time() - start)


    def load_whitelist(self, fname="whitelist.txt"):
        """Load the whitelist into the database.
        (DOES NOT unblock users. Use process_whitelist() for that.)"""

        print "Loading whitelist"

        names = []
        with open(fname) as f:
            for line in f:
                names.append(line.strip())

        chunks = chunkify(names, 100)
        users = []
        for i,chunk in enumerate(chunks):
            users += lookup_users_chunk(self.api, i, len(chunks), at_names=chunk)
        print

        for u in users:
            self.db.add_whitelist(u.id)
        self.db.commit()

        print


    def process_whitelist(self, cause_id):
        """Unblock users in the whitelist"""
        active = self.db.get_active_whitelisted(cause_id)
        for uid in active:
            self.db.whitelist_user_cause(uid, cause_id)
            self.unblock(uid)
        self.db.commit()


    def get_blocklist(self):
        """return a list of user IDs that are blocked by the logged in user"""
        return self._get_ids_paged_ratelimited(1, self.api.GetBlocksIDsPaged, {}, "/blocks/ids")


    def clear_blocklist(self, blocklist=None):
        """unblock everyone"""
        if blocklist is None:
            blocklist = self.get_blocklist()
        print "\nClearing %d blocked users..." % len(blocklist)

        pool = multiprocessing.Pool(32)
        results = []
        start = time.time()
        try:
            for i,twitter_id in enumerate(blocklist):
                results.append(pool.apply_async(simple_unblock, [self.api, twitter_id, i, len(blocklist)]))
            pool.close()
            for r in results:
                r.wait(999999999)
        except KeyboardInterrupt:
            pool.terminate()
            print
            sys.exit()
        else:
            pool.join()
        print "Unblocking of %d users completed in %.2f seconds" % (len(blocklist), time.time() - start)


    def block(self, user):
        """Block the user represented by the specified user object"""
        print "Block @%s (%d)" % (user.screen_name, user.id)

        if self.live:
            self.api.CreateBlock(user_id=user.id, include_entities=False, skip_status=True)


    def unblock(self, twitter_id):
        """Unblock the user represented by the specified user ID"""
        uname = self.db.get_atname_by_id(twitter_id)
        print "Unblock @%s (%d)" % (uname, twitter_id)

        if self.live:
            try:
                self.api.DestroyBlock(user_id=twitter_id, include_entities=False, skip_status=True)
            except twitter.error.TwitterError as e:
                e_type, e_val, e_tb = sys.exc_info()
                if error_message(e, "Sorry, that page does not exist."): #this probably means the user is suspended or deleted
                    try:
                        user = self.api.GetUser(user_id=twitter_id, include_entities=False) #if the user is suspended, this will throw an error
                        raise Exception("DestroyBlock threw \"page does not exist\" but GetUser didn't throw aything (for @%s)" % user.screen_name)

                    except twitter.error.TwitterError as e2:
                        if error_message(e2, "User has been suspended."):
                            print "\t@%s is suspended" % uname
                            self.db.set_user_deleted(twitter_id, SUSPENDED)
                            self.db.commit()

                        elif error_message(e2, "User not found."):
                            print "\t@%s is deleted" % uname
                            self.db.set_user_deleted(twitter_id, DELETED)
                            self.db.commit()

                        else:
                            traceback.print_exception(e_type, e_val, e_tb)
                            raise e2
                else:
                    raise



class FollowerBlocker (Blocker):
    """Blocks the followers of one specific user (called the root user)"""

    def __init__(self, api, db, root_at_name, live=False):
        """Initialize the object with root_at_name as the root user's twitter handle"""
        super(FollowerBlocker, self).__init__(api, db, live)
        self.root_at_name = root_at_name

        self.db.add_cause("follows", self.root_at_name)
        self.db.commit()
        self.cause_id = self.db.get_cause("follows", self.root_at_name)

        self.process_whitelist(self.cause_id)


    def __repr__(self):
        return '<%s.%s [user="%s", db="%s", root="%s"]>' % (self.__module__, self.__class__.__name__, self.username, self.db.fname, self.root_at_name)


    def get_followers(self):
        """Get the followers of the object's root user"""

        root_user = self.api.GetUser(screen_name=self.root_at_name, include_entities=False)
        print "Getting %d followers of @%s" % (root_user.followers_count, root_user.screen_name)

        print "Getting follower IDs"
        num_pages = int(math.ceil(float(root_user.followers_count)/5000))
        follower_ids = self._get_ids_paged_ratelimited(num_pages, self.api.GetFollowerIDsPaged, {"screen_name":self.root_at_name}, "/followers/ids")

        chunks = chunkify(follower_ids, 100)

        pool = multiprocessing.Pool(32)
        followers = []
        results = []

        start = time.time()

        print "\nGetting follower objects"
        try:
            for i,chunk in enumerate(chunks):
                args = [self.api, i, len(chunks)]
                kwargs = {"user_ids": chunk}
                results.append(pool.apply_async(lookup_users_chunk, args, kwargs))
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
                print "\n???"
                raise

        print "\nFollower objects gotten in %.2f seconds\n" % (time.time() - start)

        return followers


    def scan(self, followers=None):
        """Do one pass of getting the root user's followers, updating the
        database, and blocking/unblocking as necessary"""

        if followers is None:
            followers = self.get_followers()

        follower_ids = set(u.id for u in followers)
        old_followers = self.db.get_user_ids_by_cause(self.cause_id)

        whitelist = set(self.db.get_whitelist())

        to_block = []
        unblocks = 0

        # deactivate users that no longer follow the root user
        print "Calculating unblocks..."
        for uid in old_followers:
            if uid not in follower_ids:
                self.db.deactivate_user_cause(uid, self.cause_id)
                self.unblock(uid)
                unblocks += 1
        print

        # add and update users that do follow the root user
        print "Calculating blocks..."
        for follower in followers:
            whitelisted = follower.id in whitelist

            if not whitelisted and not self.db.is_user_cause_active(follower.id, self.cause_id): #only block if not whitelisted
                to_block.append(follower)

            self.db.add_user(follower) #add user regardless of whitelist status

            self.db.add_user_cause(follower.id, self.cause_id) #add cause regardless of whitelist...
            if whitelisted:
                self.db.whitelist_user_cause(follower.id, self.cause_id) #...but don't activate it if whitelisted
        print

        self.db.commit()

        self._do_block(to_block)

        print "\n%d blocks, %d unblocks\n" % (len(to_block), unblocks)



def created_at(created_at):
    """Convert a timestamp string to one more suited for sorting"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(created_at, "%a %b %d %H:%M:%S +0000 %Y"))


def chunkify(a, size):
    """Split `a` into chunks of size `size`"""
    return [a[i:i+size] for i in xrange(0, len(a), size)]


def limit_block(api, endpoint, i=-1):
    """Attempt to stop us from exceeding our API rate limit for the given endpoint.
    The API object doesn't always have complete information on our remaining
    limits (especially when multithreading), so it doesn't always work."""
    limit = api.rate_limit.get_limit(endpoint)
    if limit.remaining == 0:
        sleep_for = limit.reset - time.time() + 5
        if sleep_for <= 0:
            return

        print "\n[i=%d] Rate limit reached for %s. Sleeping for %.0f seconds (%.1f minutes. Until %s)" % (i, endpoint, sleep_for, sleep_for/60, time.strftime("%H:%M:%S", time.localtime(limit.reset+5)))
        time.sleep(sleep_for)
        print "Resuming"


def lookup_users_chunk(api, i, num_chunks, user_ids=None, at_names=None):
    """Threadable function for looking up user objects. Used by FollowerBlocker.get_followers()"""
    cr()
    write_percentage(i, num_chunks, "chunk")
    sys.stdout.flush()

    while True:
        limit_block(api, "/users/lookup", i)
        try:
            return api.UsersLookup(user_id=user_ids, screen_name=at_names, include_entities=False)
        except twitter.error.TwitterError as e:
            if error_message(e, RLE):
                continue
            else:
                threadname = multiprocessing.current_process().name
                print "\n=== %s i=%d ==========" % (threadname, i)
                traceback.print_exc()
                print e.message
                print "==========================="

                with open("error_logs/%s.log" % threadname, "w") as f:
                    traceback.print_exc(file=f)

                raise


def block_wrapper(blocker, user, i=0, total=1):
    """wrapper for Blocker.block() for threading"""
    write_percentage(i, total)
    blocker.block(user)


def simple_unblock(api, twitter_id, i=0, total=1):
    """Just unblock a user. No database stuff"""
    write_percentage(i, total)
    print "Unblock %d" % twitter_id
    try:
        api.DestroyBlock(user_id=twitter_id, include_entities=False, skip_status=True)
    except twitter.error.TwitterError as e:
        print "Error:", twitter_id, e.message


def cr():
    """carriage return and clear line"""
    sys.stdout.write("\r\x1b[K")


def write_percentage(i, total, prefix=""):
    """print a progress tracker"""
    if prefix:
        sys.stdout.write(prefix + " ")
    sys.stdout.write("%d/%d - %.4f%% : " % (i+1, total, float(i+1)*100/total))


def error_message(e, msg):
    """return whether the given twitter.error.TwitterError object has the given error message."""
    return type(e.message) == list \
        and len(e.message) > 0 \
        and type(e.message[0]) == dict \
        and "message" in e.message[0] \
        and e.message[0]["message"] == msg


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
        oauth = requests_oauthlib.OAuth1Session(consumer_key, client_secret=consumer_secret, callback_uri="oob")

        req_token = oauth.fetch_request_token("https://api.twitter.com/oauth/request_token")
        oat = req_token.get("oauth_token")
        auth_url  = oauth.authorization_url("https://api.twitter.com/oauth/authorize")

        print "\nGo to this URL to get a PIN code (make sure you're logged in as @%s):" % username
        print "\t%s\n" % auth_url

        pin = raw_input("Enter your PIN: ")
        print

        # Doing a normal pin verifier for fetch_access_token results in us
        # getting a read-only token. I don't know why. Anyhow,
        # parse_authorization_response (like you would do with a callback URL)
        # seems to work, so here we're building a fake callback URL with the pin
        # as the verifier. This gives us a read-write token like we want.
        oauth.parse_authorization_response("?oauth_token=%s&oauth_verifier=%s" % (oat, pin))

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
        print "Logged in successfully as @%s" % user.screen_name
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


def main():
    db = DatabaseAccess("database.sqlite")

    fb = FollowerBlocker(login("BlockMachine_RS"), db, "RichardBSpencer", True)
    #fb.scan()


if __name__ == "__main__":
    main()
