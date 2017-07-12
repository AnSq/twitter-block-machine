#!/usr/bin/env python

import os
import sys
import json
import twitter
import requests_oauthlib
import urllib3


CONSUMER_FILE = "consumer.json"


urllib3.disable_warnings()


def login(username, consumer_file=CONSUMER_FILE):
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
        access_token_secret=access_token_secret
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


def main():
    api = login("AnSq00")
    print api.VerifyCredentials().name


if __name__ == "__main__":
    main()
