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
UNKNOWN_ERROR = set(['Unknown error: '])
REQUEST_TIMEOUT = 60


urllib3.disable_warnings()



class User (object):
    """Simple object for storing information about a user"""

    @staticmethod
    def _replace_url_entities(user, attr):
        """replace Twitter URL entities in the given attribute of the given twitter.models.User object and returns the result"""
        s = user.__getattribute__(attr)
        try:
            for en in user._json["entities"][attr]["urls"]:
                s = s.replace(en["url"], en["expanded_url"])
        except KeyError as e:
            pass
        finally:
            return s.replace("http://", "").replace("https://", "") if s else s


    def __init__(self, user):
        """Create a new User from the given twitter.models.User"""
        self.id           = user.id
        self.at_name      = user.screen_name
        self.display_name = user.name
        self.tweets       = user.statuses_count
        self.following    = user.friends_count
        self.followers    = user.followers_count
        self.likes        = user.favourites_count
        self.verified     = user.verified
        self.protected    = user.protected
        self.bio          = self._replace_url_entities(user, "description") or None
        self.location     = user.location or None
        self.url          = self._replace_url_entities(user, "url")
        self.egg          = user.default_profile_image
        self.created_at   = created_at(user.created_at)
        self.lang         = user.lang or None
        self.time_zone    = user.time_zone
        self.utc_offset   = user.utc_offset



class DatabaseAccess (object):
    """Provides access to the database"""

    def __init__(self, fname="database.sqlite"):
        """Connect to the database, creating it if needed"""
        self.original_process = (multiprocessing.current_process().name, multiprocessing.current_process().ident)

        self.fname = fname
        self.conn = sqlite3.connect(self.fname)
        self.cur = self.conn.cursor()

        with open("schema.sql") as f:
            self.cur.executescript(f.read())
        self.cur.execute("INSERT OR IGNORE INTO deleted_types (id, name) VALUES (?, 'deleted'), (?, 'suspended');", [DELETED, SUSPENDED])
        self.commit()


    def __repr__(self):
        return '<%s.%s(fname="%s")>' % (self.__module__, self.__class__.__name__, self.fname)


    def in_original_process(self):
        """Return whether this function is called from the same process that created the object"""
        return (multiprocessing.current_process().name, multiprocessing.current_process().ident) == self.original_process


    def commit(self):
        """Commit pending changes to the database"""
        self.conn.commit()


    def rollback(self):
        """Revert pending changes to the database"""
        self.conn.rollback()


    def add_cause(self, name, cause_type, reason, bot_at_name=None, tweet_threshold=1):
        """Add a cause to the list of available causes. Does nothing if it already exists"""
        data = {
            "name"            : name,
            "cause_type"      : cause_type,
            "reason"          : reason,
            "bot_at_name"     : bot_at_name,
            "tweet_threshold" : tweet_threshold
        }

        self.cur.execute("UPDATE OR IGNORE causes SET cause_type=:cause_type, reason=:reason, bot_at_name=:bot_at_name, tweet_threshold=:tweet_threshold WHERE name=:name", data)

        try:
            self.cur.execute("INSERT OR FAIL INTO causes (name, cause_type, reason, bot_at_name, tweet_threshold) VALUES (:name, :cause_type, :reason, :bot_at_name, :tweet_threshold);", data)
        except sqlite3.IntegrityError as e:
            pass


    def add_whitelist(self, user_id, at_name):
        """Add a user to the whitelist. DOES NOT unblock them. Blockers are responsible for processing the whitelist."""
        self.cur.execute("INSERT OR IGNORE INTO whitelist (user_id, at_name) VALUES (?, ?);", [user_id, at_name])


    def clear_whitelist(self):
        """Clear the whitelist"""
        self.cur.execute("DELETE FROM whitelist;")


    def get_cause(self, name):
        """Get the ID of the given cause"""
        rs = self.cur.execute("SELECT id FROM causes WHERE name==?", [name])
        row = rs.fetchone()
        return row[0] if row else None


    def add_user(self, user):
        """Add a user to the database, or updates them if they're already in it"""

        data = {
            "user_id"      : str(user.id),
            "at_name"      : user.at_name,
            "display_name" : user.display_name,
            "tweets"       : user.tweets,
            "following"    : user.following,
            "followers"    : user.followers,
            "likes"        : user.likes,
            "verified"     : user.verified,
            "protected"    : user.protected,
            "bio"          : user.bio,
            "location"     : user.location,
            "url"          : user.url,
            "egg"          : user.egg,
            "created_at"   : user.created_at,
            "lang"         : user.lang,
            "time_zone"    : user.time_zone,
            "utc_offset"   : user.utc_offset,
            "deleted"      : 0
        }

        self.cur.execute("""
            UPDATE OR IGNORE users SET
            at_name=:at_name, display_name=:display_name, tweets=:tweets, following=:following, followers=:followers, likes=:likes, verified=:verified, protected=:protected, bio=:bio, location=:location, url=:url, egg=:egg, created_at=:created_at, lang=:lang, time_zone=:time_zone, utc_offset=:utc_offset, deleted=:deleted
            WHERE user_id=:user_id;
            """,
            data
        )

        self.cur.execute ("""
            INSERT OR IGNORE INTO users
            (        user_id,  at_name,  display_name,  tweets,  following,  followers,  likes,  verified,  protected,  bio,  location,  url,  egg,  created_at,  lang,  time_zone,  utc_offset,  deleted)
            VALUES (:user_id, :at_name, :display_name, :tweets, :following, :followers, :likes, :verified, :protected, :bio, :location, :url, :egg, :created_at, :lang, :time_zone, :utc_offset, :deleted);
            """,
            data
        )


    def add_user_cause(self, user_id, cause_id, citation=None, cite_count=None):
        """Add a user-cause relationship to the database.
        Activates the relationship if it already exists.
        Does nothing if it's already active."""
        data = {
            "user_id"    : str(user_id),
            "cause"      : cause_id,
            "citation"   : citation,
            "cite_count" : cite_count
        }
        self.cur.execute("UPDATE OR IGNORE user_causes SET citation=:citation, cite_count=:cite_count, active=1 WHERE user_id==:user_id AND cause==:cause;", data)

        try:
            self.cur.execute("INSERT OR FAIL INTO user_causes (user_id, cause, citation, cite_count) VALUES (:user_id, :cause, :citation, :cite_count);", data)
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


    def deactivate_user_cause(self, user_id, cause_id):
        """Deactivate a user-cause relationship.
        (Deactivated user-cause relationships represent users that previously
        matched a cause but don't anymore. For example, if they unfollowed the
        root user of a "follows" type cause. User-cause relationships can be
        reactivated if, for example, a user re-follows someone.)"""
        self.cur.execute("UPDATE user_causes SET active=0, removed_count=removed_count+1 WHERE user_id==? AND cause==?;", [str(user_id), cause_id])


    def whitelist_user_cause(self, user_id, cause_id):
        """Deactivate a user-cause relationship due to whitelisting.
        Does not increment removed_count."""
        self.cur.execute("UPDATE user_causes SET active=0 WHERE user_id==? AND cause==?;", [str(user_id), cause_id])


    def is_user_cause_active(self, user_id, cause_id):
        """Check if a user-cause relationship is active"""
        rs = self.cur.execute("SELECT user_id FROM user_causes WHERE user_id==? AND cause==? AND active==1;", [str(user_id), cause_id])
        return bool(rs.fetchone())


    def get_atname_by_id(self, user_id):
        """get the at_name associated with a Twitter ID"""
        rs = self.cur.execute("SELECT at_name FROM users WHERE user_id==?;", [str(user_id)])
        result = rs.fetchone()
        if result:
            return result[0]
        else:
            return None


    def get_active_whitelisted(self, cause_id):
        """Get user IDs that are active with the given cause and whitelisted."""
        rs = self.cur.execute("""
            SELECT users.user_id FROM users JOIN user_causes ON users.user_id==user_causes.user_id
            WHERE cause==? AND active==1 AND users.user_id IN (SELECT user_id FROM whitelist);
            """,
            [cause_id]
        )
        return [int(x[0]) for x in rs.fetchall()]


    def get_whitelist(self):
        """return the entire whitelist as a list"""
        rs = self.cur.execute("SELECT user_id FROM whitelist;")
        return [int(x[0]) for x in rs.fetchall()]


    def set_user_deleted(self, user_id, deleted_status):
        """mark the given user as deleted or suspended"""
        self.cur.execute("UPDATE users SET deleted=? WHERE user_id==?;", [deleted_status, str(user_id)])



class Blocker (object):
    """Base object for blockers"""

    def __init__(self, name, api, db, tweet_threshold=1, live=False):
        self.name = name
        self.api = api
        self.db = db
        self.tweet_threshold = tweet_threshold
        self.live = live
        self.load_whitelist()
        self.username = api.VerifyCredentials().screen_name


    def __repr__(self):
        return '<%s.%s [name="%s", user="%s", db="%s"]>' % (self.__module__, self.__class__.__name__, self.name, self.username, self.db.fname)


    def _get_ids_paged_ratelimited(self, num_pages, page_function, page_kwargs, endpoint):
        """Get paginated IDs, handling ratelimiting"""
        cursor = -1
        results = []

        i = 0
        while True:
            limit_block(self.api, endpoint, i)

            cr()
            if num_pages is None:
                sys.stdout.write("page %d" % (i+1))
            else:
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

        print
        return results


    def _do_block_unblock(self, users_to_process, block):
        """Block users, multithreaded.
        block=True: block users; block=False: unblock users"""
        which = "Blocking" if block else "Unblocking"
        print "%s %d users..." % (which, len(users_to_process))
        if len(users_to_process) > 0:
            pool = multiprocessing.Pool(min(32, len(users_to_process)))
            results = []
            start = time.time()
            try:
                for i,user in enumerate(users_to_process):
                    results.append(pool.apply_async(block_unblock_wrapper, [self, user, block, i, len(users_to_process)]))
                pool.close()
                for r in results:
                    r.wait(999999999)
            except KeyboardInterrupt:
                pool.terminate()
                print
                sys.exit()
            else:
                pool.join()
            print "%s completed in %.2f seconds" % (which, time.time() - start)
        print


    def _do_block(self, users_to_block):
        """Block users, multithreaded"""
        self._do_block_unblock(users_to_block, True)


    def _do_unblock(self, users_to_unblock):
        """Unblock users, multithreaded"""
        self._do_block_unblock(users_to_unblock, False)


    def _threaded_database_fix(self):
        """Replaces self.db with a new DatabaseAccess object if not in main process.
        (sqlite3 package doesn't support using the same Cursor object in multiple processes.)"""
        if not self.db.in_original_process():
            self.db = DatabaseAccess(self.db.fname)


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

        self.db.clear_whitelist()

        for u in users:
            self.db.add_whitelist(u.id, u.at_name)
        self.db.commit()


    def process_whitelist(self):
        """Unblock users in the whitelist"""
        active = self.db.get_active_whitelisted(self.cause_id)
        for uid in active:
            self.db.whitelist_user_cause(uid, self.cause_id)
            self.unblock(uid)
        self.db.commit()
        print


    def get_blocklist(self):
        """return a list of user IDs that are blocked by the logged in user"""
        return self._get_ids_paged_ratelimited(None, self.api.GetBlocksIDsPaged, {}, "/blocks/ids")


    def clear_blocklist(self, blocklist=None):
        """unblock everyone"""
        if blocklist is None:
            blocklist = self.get_blocklist()
        print "\nClearing %d blocked users..." % len(blocklist)

        pool = multiprocessing.Pool(32)
        results = []
        start = time.time()
        try:
            for i,user_id in enumerate(blocklist):
                results.append(pool.apply_async(simple_unblock, [self.api, user_id, i, len(blocklist)]))
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


    def sync_blocklist(self):
        """Make sure that the actual blocklist matches the database, blocking and unblocking as needed."""
        print "Syncing blocklist..."

        database_list = set(self.db.get_user_ids_by_cause(self.cause_id))
        twitter_list  = set(self.get_blocklist())

        to_unblock = twitter_list  - database_list
        to_block   = database_list - twitter_list

        self._do_unblock(to_unblock)
        self._do_block(to_block)

        self.db.commit()

        print "Sync finished: %d blocks, %d unblocks" % (len(to_block), len(to_unblock))


    def block(self, user):
        """Block the user represented by the specified User object, Twitter ID, or [at_name, user_id] list or tuple"""
        if type(user) is int:
            user_id = user
            self._threaded_database_fix()
            at_name = self.db.get_atname_by_id(user_id) or "???"
        elif type(user) is list or type(user) is tuple:
            at_name = user[0]
            user_id = user[1]
        else:
            at_name = user.at_name
            user_id = user.id

        print "Block @%s (%d)" % (at_name, user_id)

        try:
            if self.live:
                self.api.CreateBlock(user_id=user_id, include_entities=False, skip_status=True)
            else:
                user = self.api.GetUser(user_id=user_id, include_entities=False)
        except twitter.error.TwitterError as e:
            self._handle_deleted(at_name, user_id, e)


    def unblock(self, user_id):
        """Unblock the user represented by the specified user ID"""
        self._threaded_database_fix()
        at_name = self.db.get_atname_by_id(user_id)
        print "Unblock @%s (%d)" % (at_name, user_id)

        if self.live:
            try:
                self.api.DestroyBlock(user_id=user_id, include_entities=False, skip_status=True)
            except twitter.error.TwitterError as e:
                outer_exception_info = sys.exc_info()
                if error_message(e, "Sorry, that page does not exist."): #this probably means the user is suspended or deleted
                    try:
                        user = self.api.GetUser(user_id=user_id, include_entities=False) #if the user is suspended, this will throw an error
                        raise Exception("DestroyBlock threw \"page does not exist\" but GetUser didn't throw aything (for @%s)" % user.screen_name)
                    except twitter.error.TwitterError as inner_exception:
                        self._handle_deleted(at_name, user_id, inner_exception, outer_exception_info)
                else:
                    raise
        else:
            try:
                self.api.GetUser(user_id=user_id, include_entities=False)
            except twitter.error.TwitterError as e:
                self._handle_deleted(at_name, user_id, e)


    def _handle_deleted(self, at_name, user_id, e, outer_exception_info=None):
        """Error handling for blocking/unblocking of deleted/suspended users"""
        if not self.db.in_original_process():
            self.db = DatabaseAccess(self.db.fname)

        if error_message(e, "User has been suspended."):
            print "\tsuspended: @%s (%d)" % (at_name, user_id)
            self.db.deactivate_user_cause(user_id, self.cause_id)
            self.db.set_user_deleted(user_id, SUSPENDED)
            self.db.commit()

        elif error_message(e, "User not found."):
            print "\tdeleted: @%s (%d)" % (at_name, user_id)
            self.db.deactivate_user_cause(user_id, self.cause_id)
            self.db.set_user_deleted(user_id, DELETED)
            self.db.commit()

        else:
            if outer_exception_info:
                e1 = outer_exception_info
                traceback.print_exception(e1[0], e1[1], e1[2])
            raise e


    def filter_users(self, users):
        """returns the subset of `users` that have tweeted at least as many times as this bot's threshold and are not protected"""
        result = []
        removed_count = 0
        for u in users:
            if u.tweets >= self.tweet_threshold and not u.protected:
                result.append(u)
            else:
                removed_count += 1
        print "%d users filtered" % removed_count
        return result



class FollowerBlocker (Blocker):
    """Blocks the followers of one specific user (called the root user)"""

    def __init__(self, name, api, db, root_at_name, tweet_threshold=1, live=False):
        """Initialize the object with root_at_name as the root user's twitter handle"""
        Blocker.__init__(self, name, api, db, tweet_threshold, live)
        self.root_at_name = root_at_name

        self.db.add_cause(self.name, "follows", self.root_at_name, self.username, self.tweet_threshold)
        self.db.commit()
        self.cause_id = self.db.get_cause(self.name)

        self.process_whitelist()


    def __repr__(self):
        return '<%s.%s [name="%s", user="%s", db="%s", root="%s"]>' % (self.__module__, self.__class__.__name__, self.name, self.username, self.db.fname, self.root_at_name)


    def get_follower_ids(self, root_at_name):
        """Get the follower IDs of the given root user"""
        root_user = self.api.GetUser(screen_name=root_at_name, include_entities=False)
        print "Getting %d followers of @%s" % (root_user.followers_count, root_user.screen_name)

        print "Getting follower IDs"
        num_pages = int(math.ceil(float(root_user.followers_count)/5000))
        follower_ids = self._get_ids_paged_ratelimited(num_pages, self.api.GetFollowerIDsPaged, {"screen_name":root_at_name}, "/followers/ids")
        return follower_ids


    def get_follower_objects(self, follower_ids):
        """Get the follower objects for the given follower ids"""
        chunks = chunkify(follower_ids, 100)

        pool = multiprocessing.Pool(32)
        followers = []
        results = []

        start = time.time()

        print "Getting follower objects"
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


    def get_followers(self, root_at_name):
        """Get the followers of the given root user"""
        follower_ids = self.get_follower_ids(root_at_name)
        followers = self.get_follower_objects(follower_ids)
        return followers


    def scan(self, followers=None):
        """Do one pass of getting the root user's followers, updating the
        database, and blocking/unblocking as necessary"""

        if followers is None:
            followers = self.filter_users(self.get_followers(self.root_at_name))

        follower_ids = set(u.id for u in followers)
        old_follower_ids = set(self.db.get_user_ids_by_cause(self.cause_id))

        whitelist = set(self.db.get_whitelist())

        # deactivate users that no longer follow the root user
        print "Calculating unblocks..."
        to_unblock = old_follower_ids - follower_ids
        for uid in to_unblock:
            self.db.deactivate_user_cause(uid, self.cause_id)

        # add and update users that do follow the root user
        print "Calculating blocks..."
        to_block = []
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

        self._do_unblock(to_unblock)
        self._do_block(to_block)

        print "%d blocks, %d unblocks\n" % (len(to_block), len(to_unblock))



class MultiFollowerBlocker (FollowerBlocker):
    """Blocks the followers of multiple users."""

    def __init__(self, name, api, db, root_at_names, tweet_threshold=1, live=False):
        Blocker.__init__(self, name, api, db, tweet_threshold, live)
        self.root_at_names = root_at_names

        self.db.add_cause(self.name, "follows_any", ",".join(self.root_at_names), self.username, self.tweet_threshold)
        self.db.commit()
        self.cause_id = self.db.get_cause(self.name)

        self.process_whitelist()


    def __repr__(self):
        return '<%s.%s [name="%s", user="%s", db="%s", roots=%s]>' % (self.__module__, self.__class__.__name__, self.name, self.username, self.db.fname, str(self.root_at_names))


    def scan(self):
        """Do one pass of getting the root users' followers, updating the
        database, and blocking/unblocking as necessary"""
        follower_ids = {}
        for root_at_name in self.root_at_names:
            follower_ids[root_at_name] = set(self.get_follower_ids(root_at_name))

        all_follower_ids = set()
        for root_at_name in follower_ids:
            all_follower_ids |= follower_ids[root_at_name]
        all_follower_ids = list(all_follower_ids)

        t = len(all_follower_ids)
        print t

        follower_objects = self.filter_users(self.get_follower_objects(all_follower_ids))
        all_follower_ids = set(u.id for u in follower_objects)

        old_follower_ids = set(self.db.get_user_ids_by_cause(self.cause_id))

        whitelist = set(self.db.get_whitelist())

        print "Calculating unblocks..."
        to_unblock = old_follower_ids - all_follower_ids
        del old_follower_ids
        del all_follower_ids
        for uid in to_unblock:
            self.db.deactivate_user_cause(uid, self.cause_id)

        print "Calculating blocks..."
        to_block = []
        for follower in follower_objects:
            whitelisted = follower.id in whitelist

            if not whitelisted and not self.db.is_user_cause_active(follower.id, self.cause_id): #only block if not whitelisted
                to_block.append((follower.at_name,follower.id))

            self.db.add_user(follower) #add user regardless of whitelist status

            citation = []
            for root_at_name in follower_ids:
                if follower.id in follower_ids[root_at_name]:
                    citation.append(root_at_name)
            cite_count = len(citation)
            citation = ",".join(citation)

            self.db.add_user_cause(follower.id, self.cause_id, citation, cite_count) #add cause regardless of whitelist...
            if whitelisted:
                self.db.whitelist_user_cause(follower.id, self.cause_id) #...but don't activate it if whitelisted
        print

        del follower_ids
        del follower_objects
        del whitelist

        self.db.commit()

        self._do_unblock(to_unblock)
        self._do_block(to_block)

        print "%d blocks, %d unblocks\n" % (len(to_block), len(to_unblock))



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
            return [User(x) for x in api.UsersLookup(user_id=user_ids, screen_name=at_names, include_entities=False)]
        except twitter.error.TwitterError as e:
            if error_message(e, RLE) or error_message(e, "Over capacity") or e.message == UNKNOWN_ERROR:
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


def block_unblock_wrapper(blocker, user, block, i=0, total=1):
    """wrapper of Blocker.block() and Blocker.unblock() for threading.
    block=True: block user; block=False: unblock user """
    write_percentage(i, total)
    sys.stdout.write(" : ")
    try:
        if block:
            blocker.block(user)
        else:
            blocker.unblock(user)
    except Exception as e:
        threadname = multiprocessing.current_process().name
        fname = "error_logs/%s.log" % threadname
        with open(fname, "w") as f:
            traceback.print_exc(file=f)
        print e, "Logged to %s" % fname


def simple_unblock(api, user_id, i=0, total=1):
    """Just unblock a user. No database stuff"""
    write_percentage(i, total)
    print " : Unblock %d" % user_id
    try:
        api.DestroyBlock(user_id=user_id, include_entities=False, skip_status=True)
    except twitter.error.TwitterError as e:
        print "Error:", user_id, e.message


def cr():
    """carriage return and clear line"""
    sys.stdout.write("\r\x1b[K")


def write_percentage(i, total, prefix=""):
    """print a progress tracker"""
    if prefix:
        sys.stdout.write(prefix + " ")
    sys.stdout.write("%d/%d - %.2f%%" % (i+1, total, float(i+1)*100/total))


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
        timeout=REQUEST_TIMEOUT,
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

    a = [5]
    if 1 in a:
        rs = FollowerBlocker("Richard Spencer", login("BlockMachine_RS"), db, "RichardBSpencer", 1, True)
        #rs.scan()
        rs.sync_blocklist()
    if 2 in a:
        dd = FollowerBlocker("David Duke", login("BlockMachine_DD"), db, "DrDavidDuke", 1, True)
        #dd.scan()
        dd.sync_blocklist()
    if 3 in a:
        jd = FollowerBlocker("James Damore", login("BlockMachine_JD"), db, "JamesADamore", 1, True)
        #jd.scan()
        jd.sync_blocklist()
    if 4 in a:
        root_at_names = [
            "BryanJFischer",
            "DavidBartonWB",
            "GDavidLane",
            "garydemar",
            "LouEngle",
            "tperkins",
            "AmericanFamAssc",
            "AFAAction",
            "AllianceDefends",
            "FRCdc",
            "FRCAction",
            "libertycounsel",
            "MatStaver",
            "TVC_CapitolHill",
            "WBCSaysRepent",
            "PeterLaBarbera",
            "AmericanVision",
            "ATLAHWorldwide",
            "DrJamesDManning",
            "austinruse",
            "FridayFax",
            "RJRushdoony",
            "sanderson1611",
            "ProFamilyIFI",
            "ILfamilyaction",
            "MassResistance",
            "PacificJustice",
            "PublicFreedom",
            "eugenedelgaudio",
            "savecalifornia",
            "UFI",
            "ProFam_Org"
        ]
        al = MultiFollowerBlocker("anti-LGBT", login("BlockMachine_AL"), db, root_at_names, 1, True)
        al.scan()
        #al.sync_blocklist()
    if 5 in a:
        sm = FollowerBlocker("Stefan Molyneux", login("BlockMachine_SM"), db, "StefanMolyneux", 5, True)
        sm.scan()
        #sm.sync_blocklist()


if __name__ == "__main__":
    main()
