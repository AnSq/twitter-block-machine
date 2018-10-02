#!/usr/bin/env python3

import os
import sys
import json
import time
import math
import sqlite3
import multiprocessing
import multiprocessing.pool
import threading
import queue
import traceback
import itertools
import re

import requests
import requests_oauthlib
import twitter


CONSUMER_FILE = "consumer.json"
ROOT_USERS_FILE = "root_users.cfg"
SQLITE_PCRE = "/usr/lib/sqlite3/pcre.so"

REQUEST_TIMEOUT = 60
MAX_THREADS = 1
DELETED = "deleted"
SUSPENDED = "suspended"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# errors
RLE = "Rate limit exceeded"
UNKNOWN_ERROR = set(['Unknown error: '])
SUSPENDED_ERROR = "User has been suspended."
USER_NOT_FOUND = "User not found."
INTERNAL_ERROR = "Internal error"
OVER_CAPACITY = "Over capacity"



class User:
    """Simple object for storing information about a user"""

    @staticmethod
    def _replace_url_entities(user, attr):
        """replace Twitter URL entities in the given attribute of the given twitter.models.User
        object and returns the result"""
        s = user.__getattribute__(attr)
        try:
            for en in user._json["entities"][attr]["urls"]:
                s = s.replace(en["url"], en["expanded_url"])
        except KeyError:
            pass
        finally:
            return s.replace("http://", "").replace("https://", "") if s else s


    def __init__(self, user):
        """Create a new User from the given twitter.models.User"""
        self.id              = user.id
        self.at_name         = user.screen_name
        self.display_name    = user.name
        self.tweets          = user.statuses_count
        self.following       = user.friends_count
        self.followers       = user.followers_count
        self.likes           = user.favourites_count
        self.verified        = user.verified
        self.protected       = user.protected
        self.bio             = self._replace_url_entities(user, "description") or None
        self.location        = user.location or None
        self.url             = self._replace_url_entities(user, "url")
        self.egg             = user.default_profile_image
        self.created_at      = created_at(user.created_at)
        self.lang            = user.lang or None
        self.last_tweet_time = created_at(user.status.created_at) if user.status else None
        self.updated_at      = time_now()


    def __repr__(self):
        return '<{}{} @{} "{}" #{}>'.format(module_repr(self), cls_repr(self), self.at_name, self.display_name, self.id)



class DatabaseAccess:
    """Provides access to the database"""

    def __init__(self, fname="database.sqlite"):
        """Connect to the database, creating it if needed"""
        self.original_process = (multiprocessing.current_process().name, multiprocessing.current_process().ident)

        self.fname = fname
        self.conn = sqlite3.connect(self.fname)
        self.cur = self.conn.cursor()

        #self.conn.create_function("REGEXP", 2, self._function_regexp)
        self.conn.enable_load_extension(True)
        self.conn.load_extension(SQLITE_PCRE)

        with open("schema.sql") as f:
            self.cur.executescript(f.read())
        self.commit()


    def __repr__(self):
        return '<{}{}(fname="{}")>'.format(module_repr(self), cls_repr(self), self.fname)


    @staticmethod
    def _function_regexp(pattern, string):
        if not string:
            return False
        return re.search(pattern, string, re.IGNORECASE) is not None


    def in_original_process(self):
        """Return whether this function is called from the same process that created the object"""
        return (multiprocessing.current_process().name, multiprocessing.current_process().ident) == self.original_process


    def commit(self):
        """Commit pending changes to the database"""
        self.conn.commit()


    def rollback(self):
        """Revert pending changes to the database"""
        self.conn.rollback()


    def add_whitelist(self, user_id, at_name):
        """Add a user to the whitelist. DOES NOT unblock them."""
        self.cur.execute("INSERT OR IGNORE INTO whitelist (user_id, at_name) VALUES (?, ?);", [user_id, at_name])


    def clear_whitelist(self):
        """Clear the whitelist"""
        self.cur.execute("DELETE FROM whitelist;")


    def add_user(self, user):
        """Add a user to the database, or updates them if they're already in it"""

        data = {
            "user_id"         : str(user.id),
            "at_name"         : user.at_name,
            "display_name"    : user.display_name,
            "tweets"          : user.tweets,
            "following"       : user.following,
            "followers"       : user.followers,
            "likes"           : user.likes,
            "verified"        : user.verified,
            "protected"       : user.protected,
            "bio"             : user.bio,
            "location"        : user.location,
            "url"             : user.url,
            "egg"             : user.egg,
            "created_at"      : user.created_at,
            "lang"            : user.lang,
            "last_tweet_time" : user.last_tweet_time,
            "deleted"         : None,
            "updated_at"      : user.updated_at
        }

        self.cur.execute("""
            UPDATE OR IGNORE users SET
            at_name=:at_name, display_name=:display_name, tweets=:tweets,
            following=:following, followers=:followers, likes=:likes,
            verified=:verified, protected=:protected, bio=:bio,
            location=:location, url=:url, egg=:egg, created_at=:created_at,
            lang=:lang, last_tweet_time=:last_tweet_time, deleted=:deleted,
            updated_at=:updated_at
            WHERE user_id=:user_id;
            """,
            data
        )

        self.cur.execute ("""
            INSERT OR IGNORE INTO users
            (        user_id,  at_name,  display_name,  tweets,  following,  followers,  likes,  verified,  protected,  bio,  location,  url,  egg,  created_at,  lang,  last_tweet_time,  deleted,  updated_at)
            VALUES (:user_id, :at_name, :display_name, :tweets, :following, :followers, :likes, :verified, :protected, :bio, :location, :url, :egg, :created_at, :lang, :last_tweet_time, :deleted, :updated_at);
            """,
            data
        )


    def add_follow(self, followee_id, follower_id, active, checked_at):
        """Add or update a follower relationship to the database"""

        data = {
            "followee"   : followee_id,
            "follower"   : follower_id,
            "active"     : active,
            "checked_at" : checked_at
        }

        self.cur.execute("""
            UPDATE OR IGNORE follows SET
            active=:active, checked_at=:checked_at
            WHERE followee=:followee AND follower=:follower;
            """,
            data
        )

        self.cur.execute("""
            INSERT OR IGNORE INTO follows
            (        followee,  follower,  active,  checked_at)
            VALUES (:followee, :follower, :active, :checked_at);
            """,
            data
        )


    def update_inactive_follows(self, followee_id):
        """For follow relationships that have a checked_at time BEFORE the followee's followers_updated_at time, mark the relationship as inactive"""
        pass #TODO


    def add_block(self, blocker_id, blocked_id):
        """Add a block relationship"""
        self.cur.execute("INSERT OR IGNORE INTO blocks (blocker, blocked) VALUES (?,?);", [blocker_id, blocked_id])


    def get_atname_by_id(self, user_id):
        """get the at_name associated with a Twitter ID. Result will be None if the user is not in the database"""
        rs = self.cur.execute("SELECT at_name FROM users WHERE user_id==?;", [str(user_id)])
        result = rs.fetchone()
        if result:
            return result[0]
        else:
            return None


    def get_user_id(self, at_name):
        """get the user_id of the user with the given at_name. Result will be None if the user is not in the database"""
        rs = self.cur.execute("SELECT user_id FROM users WHERE at_name==? COLLATE NOCASE;", [at_name])
        result = rs.fetchone()
        if result:
            return str(result[0])
        else:
            return None


    def get_whitelist(self):
        """return the entire whitelist as a list"""
        rs = self.cur.execute("SELECT user_id FROM whitelist;")
        return [int(x[0]) for x in rs.fetchall()]


    def set_user_deleted(self, user_id, deleted_status):
        """mark the given user as deleted or suspended"""
        self.cur.execute("UPDATE users SET deleted=? WHERE user_id==?;", [deleted_status, str(user_id)])


    def add_root_user(self, user, comment=None):
        """add a root user entry, or update a root user's comment"""

        data = {
            "user_id" : user.id,
            "at_name" : user.at_name,
            "comment" : comment
        }

        if comment is None:
            self.cur.execute("""
                INSERT OR IGNORE INTO root_users
                (        user_id,  at_name)
                VALUES (:user_id, :at_name);
                """,
                data
            )
        else:
            self.cur.execute("""
                INSERT OR IGNORE INTO root_users
                (        user_id,  at_name,  comment)
                VALUES (:user_id, :at_name, :comment);
                """,
                data
            )

            self.cur.execute("""
                UPDATE OR IGNORE root_users
                SET comment=:comment, at_name=:at_name
                WHERE user_id=:user_id;
                """,
                data
            )


    def set_root_user_followers_updated_at(self, user_id, followers_updated_at=None):
        """update the followers_updated_at time for a root user"""
        if followers_updated_at is None:
            followers_updated_at = time_now()
        self.cur.execute("UPDATE root_users SET followers_updated_at=? WHERE user_id=?", [followers_updated_at, user_id])


    def get_new_root_users(self):
        """get the at_name of root users that have never been updated"""
        rs = self.cur.execute("SELECT at_name FROM root_users WHERE followers_updated_at IS NULL;")
        return [x[0] for x in rs.fetchall()]


    def search_users(self, terms):
        """Get user_ids matching a set of search terms.
        A user will be included in the result if it matches at
        least one of the terms (i.e., they are ORed together)"""
        fields = ["at_name", "display_name", "bio", "url"]
        query = "SELECT user_id FROM users WHERE 0"
        params = []
        for t in terms:
            for i,field in enumerate(fields):
                if terms[t][i]:
                    query += " OR coalesce(lower({}),'') REGEXP ?".format(field)
                    params.append(t)
        query += ";"

        rs = self.cur.execute(query, params)
        while True:
            chunk = rs.fetchmany(256)
            if not chunk:
                break
            for r in chunk:
                yield r[0]


    def search_users_test(self, terms):
        fields = ["at_name", "display_name", "bio", "url"]
        query = "SELECT user_id FROM users WHERE coalesce(lower({}),'') REGEXP ?"
        for t in terms:
            print()
            print('"{}",'.format(t), end="", flush=True)
            for i,field in enumerate(fields):
                if terms[t][i]:
                    rs = self.cur.execute(query.format(field), (t,))
                    print("{},".format(len(rs.fetchall())), end="", flush=True)
                else:
                    print(",", end="", flush=True)
        print()



class BlockMachine:
    def __init__(self, api, db, tweet_threshold=1, live=False):
        self.api = api
        self.db = db
        self.tweet_threshold = tweet_threshold
        self.live = live
        self.logged_in_user = User(api.VerifyCredentials())
        self.username = self.logged_in_user.at_name


    def __repr__(self):
        return '<{}{} [user="{}", db="{}"]>'.format(module_repr(self), cls_repr(self), self.username, self.db.fname)


    def _get_ids_paged_ratelimited(self, num_pages, page_function, page_kwargs, endpoint):
        """Get paginated IDs, handling ratelimiting"""
        cursor = -1

        i = 0
        while True:
            self._rate_limit(endpoint, i)

            cr()
            if num_pages is None:
                print("page {}".format(i+1))
            else:
                write_percentage(i, num_pages, "page")
                print()
            sys.stdout.flush()

            try:
                cursor, _prev_cursor, chunk = page_function(cursor=cursor, **page_kwargs)
            except twitter.error.TwitterError as e:
                if error_message(e, RLE):
                    continue
                else:
                    raise

            for user_id in chunk:
                yield user_id

            i += 1

            if cursor == 0:
                break


    def _rate_limit(self, endpoint, i=-1):
        """Attempt to stop us from exceeding our API rate limit for the given endpoint.
        The API object doesn't always have complete information on our remaining
        limits (especially when multithreading), so it doesn't always work."""
        limit = self.api.rate_limit.get_limit(endpoint)
        if limit.remaining == 0:
            sleep_for = limit.reset - time.time() + 5
            if sleep_for <= 0:
                return

            print("\n[i=%d] Rate limit reached for %s. Sleeping for %.0f seconds (%.1f minutes. Until %s)" % (i, endpoint, sleep_for, sleep_for/60, time.strftime("%H:%M:%S", time.localtime(limit.reset+5))))
            time.sleep(sleep_for)
            print("Resuming")


    def _do_block_unblock(self, users_to_process, block):
        """Block users, multithreaded.
        block=True: block users; block=False: unblock users"""
        which = "Blocking" if block else "Unblocking"
        print("%s %d users..." % (which, len(users_to_process)))
        if users_to_process:
            pool = multiprocessing.Pool(min(MAX_THREADS, len(users_to_process)))
            results = []
            start = time.time()
            try:
                for i,user in enumerate(users_to_process):
                    results.append(pool.apply_async(self._block_unblock_wrapper, [user, block, i, len(users_to_process)]))
                pool.close()
                for r in results:
                    r.wait(999999999)
            except KeyboardInterrupt:
                pool.terminate()
                print()
                sys.exit()
            else:
                pool.join()
            print("%s completed in %.2f seconds" % (which, time.time() - start))
        print()


    def _block_unblock_wrapper(self, user, block, i=0, total=1):
        """wrapper of Blocker.block() and Blocker.unblock() for threading.
        block=True: block user; block=False: unblock user """
        write_percentage(i, total)
        sys.stdout.write(" : ")
        try:
            if block:
                self.block(user)
            else:
                self.unblock(user)
        except Exception as e:
            threadname = multiprocessing.current_process().name
            fname = "error_logs/block_unblock_wrapper-Exception-%s-i%d.log" % (threadname, i)
            with open(fname, "w") as f:
                traceback.print_exc(file=f)
            print(e, "Logged to %s" % fname)


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


    def get_blocklist(self):
        """return a generator of user IDs that are blocked by the logged in user"""
        return self._get_ids_paged_ratelimited(None, self.api.GetBlocksIDsPaged, {}, "/blocks/ids")


    def add_blocklist_to_database(self):
        """Add block relationships for all of the logged in user's blocks to the database"""
        blocker_id = self.logged_in_user.id

        start_fetch = time.time()

        for blocked_id in self.get_blocklist():
            self.db.add_block(blocker_id, blocked_id)

        print("time spent fetching: {}".format(time.time() - start_fetch))
        start_commit = time.time()

        self.db.commit()

        print("time spent committing: {}".format(time.time() - start_commit))
        print("total time: {}".format(time.time() - start_fetch))


    def clear_blocklist(self, blocklist=None):
        """unblock everyone"""
        if blocklist is None:
            blocklist = self.get_blocklist()
        print("\nClearing %d blocked users..." % len(blocklist))

        pool = multiprocessing.Pool(MAX_THREADS)
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
            print()
            sys.exit()
        else:
            pool.join()
        print("Unblocking of %d users completed in %.2f seconds" % (len(blocklist), time.time() - start))


    def block(self, user):
        """Block the user represented by the specified User object,
        Twitter ID, or [at_name, user_id] list or tuple"""
        if isinstance(user, int):
            user_id = user
            self._threaded_database_fix()
            at_name = self.db.get_atname_by_id(user_id) or "???"
        elif isinstance(user, (list, tuple)):
            at_name = user[0]
            user_id = user[1]
        else:
            at_name = user.at_name
            user_id = user.id

        print("Block @%s (%d)" % (at_name, user_id))

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
        print("Unblock @%s (%d)" % (at_name, user_id))

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
            print("\tsuspended: @%s (%d)" % (at_name, user_id))
            self.db.set_user_deleted(user_id, SUSPENDED)
            self.db.commit()

        elif error_message(e, "User not found."):
            print("\tdeleted: @%s (%d)" % (at_name, user_id))
            self.db.set_user_deleted(user_id, DELETED)
            self.db.commit()

        else:
            if outer_exception_info:
                e1 = outer_exception_info
                traceback.print_exception(e1[0], e1[1], e1[2])
            raise e


    def filter_users(self, users):
        """returns the subset of `users` that have tweeted at least
        as many times as this bot's threshold and are not protected"""
        result = []
        removed_count = 0
        for u in users:
            if u.tweets >= self.tweet_threshold and not u.protected:
                result.append(u)
            else:
                removed_count += 1
        print("%d users filtered" % removed_count)
        return result


    def get_followers(self, root_at_name):
        """Get the followers of the given root user"""
        try:
            root_user = self.api.GetUser(screen_name=root_at_name, include_entities=False)
        except twitter.error.TwitterError as e:
            if error_message(e, USER_NOT_FOUND):
                print("@{} is deleted".format(root_at_name))
            elif error_message(e, SUSPENDED_ERROR):
                print("@{} is suspended".format(root_at_name))
            else:
                traceback.print_exc()
            #print("Exiting")
            #sys.exit() # TODO: some reasonable error handling here?
            return []

        followers_count = root_user.followers_count

        print("Getting {} followers of @{}".format(followers_count, root_user.screen_name))

        followers = self.get_user_objects(self.get_follower_ids, (root_at_name, followers_count), followers_count)
        return followers


    def get_follower_ids(self, root_at_name, followers_count):
        """Get the follower IDs of the given root user"""
        print("Getting follower IDs")
        num_pages = int(math.ceil(float(followers_count)/5000))
        follower_ids = self._get_ids_paged_ratelimited(num_pages, self.api.GetFollowerIDsPaged, {"screen_name":root_at_name}, "/followers/ids")
        return follower_ids


    def get_user_objects(self, user_ids_function, user_ids_function_args, user_count):
        """Get the user objects for the user ids generated by user_ids_function"""
        chunk_size = 100
        num_chunks = math.ceil(user_count / chunk_size)

        pool = multiprocessing.pool.ThreadPool(MAX_THREADS)
        #q = multiprocessing.Manager().Queue()
        q = queue.Queue()

        start = time.time()
        print("Getting user objects")
        try:
            args = (user_ids_function, user_ids_function_args, chunk_size, num_chunks, pool, q)
            spawner = threading.Thread(target=self._spawn_lookup_threads, name="_spawn_lookup_threads", args=args)
            spawner.start()
        except KeyboardInterrupt:
            pool.terminate()
            spawner.terminate()
            print()
            sys.exit()
        else:
            n = 0
            for _ in range(num_chunks):
                users = q.get()
                for user in users:
                    n += 1
                    yield user
            pool.join()
            spawner.join()

        print("\n{} user objects gotten in {:.2f} seconds\n".format(n, time.time() - start))
        print(user_count)


    def _spawn_lookup_threads(self, user_ids_function, user_ids_function_args, chunk_size, num_chunks, pool, q):
        """A thread to spawn _lookup_users_chunk threads.
        "Isn't that incredibly stupid?" you say. Yes, it is. But it's because
        get_follower_ids (which get_followers passes in as user_ids_function
        (via get_user_objects)) blocks, and we don't want get_user_objects to
        block before we start extracting stuff from the queue.
        """
        try:
            user_ids = user_ids_function(*user_ids_function_args)
            for i in range(num_chunks):
                chunk = list(itertools.islice(user_ids, chunk_size))
                args = [q, i, num_chunks]
                kwargs = {"user_ids": chunk}
                pool.apply_async(self._lookup_users_chunk, args, kwargs)
            pool.close()
        except Exception:
            traceback.print_exc()


    def _lookup_users_chunk(self, q, i, num_chunks, user_ids=None, at_names=None):
        """Threadable function for looking up user objects"""
        cr()
        write_percentage(i, num_chunks, "chunk")
        sys.stdout.flush()

        if not user_ids and not at_names:
            print(" empty chunk", i) #DEBUG
            q.put([]) # main thread keeps a count of how many it's processed, so we need this empty element
            return

        while True:
            self._rate_limit("/users/lookup", i)
            threadname = multiprocessing.current_process().name
            try:
                users = [User(x) for x in self.api.UsersLookup(user_id=user_ids, screen_name=at_names, include_entities=False)]
                q.put(users)
                return
            except twitter.error.TwitterError as e:
                if error_message(e, RLE) or error_message(e, OVER_CAPACITY) or error_message(e, INTERNAL_ERROR) or e.message == UNKNOWN_ERROR:
                    # ignore error entirely and retry
                    continue
                else:
                    # log error and retry
                    print("\n=== %s i=%d ==========" % (threadname, i))
                    traceback.print_exc()
                    print(e.message)
                    print("===========================")
                    with open("error_logs/lookup_users_chunk-TwitterError-%s-i%d.log" % (threadname, i), "w") as f:
                        traceback.print_exc(file=f)
                    continue
            except requests.exceptions.ConnectionError as e:
                fname = "error_logs/lookup_users_chunk-ConnectionError-%s-i%d.log" % (threadname, i)
                with open(fname, "w") as f:
                    print("[{}] [i={}] ConnectionError logged to '{}'. Retrying in 10 seconds.".format(time_now(), i, fname))
                    traceback.print_exc(file=f)
                time.sleep(10)
                continue


    def get_user(self, at_name):
        """get a single user. Will raise an error for deleted/suspended users"""
        return User(self.api.GetUser(screen_name=at_name, include_entities=False))


    def add_user(self, at_name):
        """add a user to the database, taking care of deleted and suspended users."""
        user = None
        try:
            user = self.get_user(at_name)
        except twitter.error.TwitterError as e:
            user_id = self.db.get_user_id(at_name)
            if error_message(e, SUSPENDED_ERROR):
                if user_id:
                    self.db.set_user_deleted(user_id, SUSPENDED)
                else:
                    print("@{} is not in the database and is suspended".format(at_name))
            elif error_message(e, USER_NOT_FOUND):
                if user_id:
                    self.db.set_user_deleted(user_id, DELETED)
                else:
                    print("@{} is not in the database and is deleted/non-existent".format(at_name))
            else:
                raise e
        else:
            self.db.add_user(user)
        finally:
            self.db.commit()
            return user


    def add_root_user(self, at_name, comment=None):
        """add a root user"""
        user = self.add_user(at_name)
        if user:
            self.db.add_root_user(user, comment)
            self.db.commit()
        print("root user: {}".format(user))
        return user


    def load_root_users(self, fname=ROOT_USERS_FILE):
        """load root users from file"""
        with open(fname) as f:
            for line in f:
                line = line.strip()

                if not line or line.startswith("#"):
                    continue

                at_name, comment = line.split(maxsplit=1)
                self.add_root_user(at_name, comment)


    def update_root_user_followers(self, at_name):
        """add the followers of a root user to the database"""
        root_user = self.add_root_user(at_name)
        if root_user:
            followers = self.get_followers(at_name)

            if followers:
                followers_updated_at = time_now()

                for i,follower in enumerate(followers):
                    self.db.add_user(follower)
                    self.db.add_follow(root_user.id, follower.id, True, followers_updated_at)

                    if i % 1000 == 0:
                        #print(" commit", i) #DEBUG
                        self.db.commit()

                self.db.set_root_user_followers_updated_at(root_user.id, followers_updated_at)
                self.db.update_inactive_follows(root_user.id)
                self.db.commit()
            else:
                print("@{} has no followers".format(at_name))



def created_at(timestamp):
    """Convert a timestamp string to one more suited for sorting"""
    return time.strftime(TIME_FORMAT, time.strptime(timestamp, "%a %b %d %H:%M:%S +0000 %Y"))


def time_now():
    """Return the time string for right now"""
    return time.strftime(TIME_FORMAT, time.gmtime())


def chunkify(a, size):
    """Split `a` into chunks of size `size`"""
    return [a[i:i+size] for i in range(0, len(a), size)]


def simple_unblock(api, user_id, i=0, total=1):
    """Just unblock a user. No database stuff"""
    write_percentage(i, total)
    print(" : Unblock %d" % user_id)
    try:
        api.DestroyBlock(user_id=user_id, include_entities=False, skip_status=True)
    except twitter.error.TwitterError as e:
        print("Error:", user_id, e.message)


def error_message(e, msg):
    """return whether the given twitter.error.TwitterError object has the given error message."""
    return isinstance(e.message, list) \
        and len(e.message) > 0 \
        and isinstance(e.message[0], dict) \
        and "message" in e.message[0] \
        and e.message[0]["message"] == msg


def cr():
    """carriage return and clear line"""
    sys.stdout.write("\r\x1b[K")


def write_percentage(i, total, prefix=""):
    """print a progress tracker"""
    if prefix:
        sys.stdout.write(prefix + " ")
    sys.stdout.write("%d/%d - %.2f%%" % (i+1, total, float(i+1)*100/total))


def module_repr(obj):
    """the name of a module. for use in __repr__"""
    return (obj.__module__ + ".") if obj.__module__ != "__main__" else ""


def cls_repr(obj):
    """the name of a class. for use in __repr__"""
    return obj.__class__.__name__


def login(username, consumer_file=CONSUMER_FILE, sleep=True):
    """Login to Twitter with the specified username.
    consumer_file is the name of a JSON file with the app's consumer key and secret.
    sleep is passed to the API wrapper as sleep_on_rate_limit."""

    print("Logging in @%s" % username)

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

        print("\nGo to this URL to get a PIN code (make sure you're logged in as @%s):" % username)
        print("\t%s\n" % auth_url)

        pin = input("Enter the PIN you got from the link above: ")
        print()

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
        print("\nLogged in user is @%s, not @%s. Exiting." % (user.screen_name, username))
        sys.exit()
    else:
        print("Logged in successfully as @%s" % user.screen_name)
        if not os.path.exists(user_fname):
            user_token = {
                "access_token":         access_token,
                "access_token_secret" : access_token_secret
            }
            with open(user_fname, "w") as f:
                json.dump(user_token, f)
            print("User token saved to %s" % user_fname)
        print()

    return api


def main():
    api = login(sys.argv[1])
    db = DatabaseAccess()
    bm = BlockMachine(api, db)

    #bm.load_root_users()

    #bm.update_root_user_followers(sys.argv[2])

    #for at_name in bm.db.get_new_root_users():
    #    bm.update_root_user_followers(at_name)

    bm.add_blocklist_to_database()


if __name__ == "__main__":
    main()
elif sys.argv[0] == "": #interactive mode
    _username = input("Log in to twitter as (default @BlockMachine_01): @")
    if not _username:
        _username = "BlockMachine_01"
    _api = login(_username)
    _db = DatabaseAccess()
    __builtins__["bm"] = BlockMachine(_api, _db)
    print("A {}.BlockMachine object is available as the varable `bm`".format(__name__))

    def _r():
        import importlib
        module = importlib.reload(sys.modules[__name__])
        __builtins__[__name__] = module
        return module
