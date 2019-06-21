# SPDX-License-Identifier: AGPL-3.0-or-later
import codecs
try:
    import ujson as json
except ImportError: # pragma: no cover
    import json
import logging
import traceback
import uuid
try:
    from queue import Queue
except ImportError: # pragma: no cover
    from Queue import Queue
import requests
from threading import Thread

INDENT = True
try:
    json.dumps({}, indent=True) # ujson 1.33 doesn't support 'indent'
except TypeError: # pragma: no cover
    INDENT = False

import falcon
from falcon import routing

try:
    from prometheus_client import Counter, Gauge
    PROMETHEUS = True
except ImportError: # pragma: no cover
    PROMETHEUS = False

from MAPI.Struct import (
    MAPIErrorNetworkError, MAPIErrorEndOfSession, MAPIErrorUnconfigured
)

import kopano
kopano.set_bin_encoding('base64')

from . import utils

logging.getLogger("requests").setLevel(logging.WARNING)

# TODO don't block on sending updates
# TODO async subscription validation
# TODO restarting app/server?
# TODO subscription expiration?
# TODO avoid globals (threading)
# TODO list subscription scalability

SUBSCRIPTIONS = {}

PATTERN_MESSAGES = (routing.compile_uri_template('/me/mailFolders/{folderid}/messages')[1], 'Message')
PATTERN_CONTACTS = (routing.compile_uri_template('/me/contactFolders/{folderid}/contacts')[1], 'Contact')
PATTERN_EVENTS = (routing.compile_uri_template('/me/calendars/{folderid}/events')[1], 'Event')

if PROMETHEUS:
    SUBSCR_COUNT = Counter('kopano_mfr_total_subscriptions', 'Total number of subscriptions')
    SUBSCR_ACTIVE = Gauge('kopano_mfr_active_subscriptions', 'Number of active subscriptions', multiprocess_mode='livesum')
    POST_COUNT = Counter('kopano_mfr_total_webhook_posts', 'Total number of webhook posts')

def _server(auth_user, auth_pass, oidc=False, reconnect=False):
    # return global connection, using credentials from first user to
    # authenticate, and use it for all notifications
    global SERVER

    try: # TODO thread lock?
        SERVER
    except NameError:
        reconnect=True

    if reconnect:
        SERVER = kopano.Server(auth_user=auth_user, auth_pass=auth_pass,
            notifications=True, parse_args=False, oidc=oidc)
        logging.info('server connection established, server:%s auth_user:%s', SERVER, SERVER.auth_user)

    return SERVER

def _user(req, options, reconnect=False, force_reconnect=False):
    auth = utils._auth(req, options)

    if auth['method'] == 'bearer':
        username = auth['user']
        server = _server(auth['userid'], auth['token'], oidc=True, reconnect=reconnect or force_reconnect)
    elif auth['method'] == 'basic':
        username = codecs.decode(auth['user'], 'utf8')
        server = _server(username, auth['password'], reconnect=reconnect or force_reconnect)
    elif auth['method'] == 'passthrough': # pragma: no cover
        username = utils._username(auth['userid'])
        server = _server(username, '', reconnect=reconnect or force_reconnect)
    try:
        user = server.user(username)
    except (MAPIErrorNetworkError, MAPIErrorEndOfSession): # server restart: try to reconnect TODO check kc_session_restore (incl. notifs!)
        if not reconnect:
            logging.exception('network or session error while getting user from server, reconnecting automatically')
            user = _user(req, options, reconnect=True)
        else:
            raise

    return user

def _user_and_store(req, options):
    user = _user(req, options)
    try:
        return user, user.store
    except MAPIErrorUnconfigured:
        user = _user(req, options, force_reconnect=True)
        return user, user.store

class Processor(Thread):
    def __init__(self, options):
        Thread.__init__(self)
        self.options = options
        self.daemon = True

    def _notification(self, subscription, event_type, obj):
        return {
            'subscriptionId': subscription['id'],
            'clientState': subscription['clientState'],
            'changeType': event_type,
            'resource': subscription['resource'],
            'resourceData': {
                '@data.type': '#Microsoft.Graph.%s' % subscription['_datatype'],
                'id': obj.eventid if subscription['_datatype'] == 'Event' else obj.entryid,
            }
        }

    def run(self):
        while True:
            store, notification, subscription = QUEUE.get()

            data = self._notification(subscription, notification.event_type, notification.object)

            verify = not self.options or not self.options.insecure
            try:
                if self.options and self.options.with_metrics:
                    POST_COUNT.inc()
                logging.debug('subscription notification: %s', subscription['notificationUrl'])
                requests.post(subscription['notificationUrl'], json=data, timeout=10, verify=verify)
            except Exception:
                logging.exception('subscription notification: %s failed', subscription['notificationUrl'])

class Sink:
    def __init__(self, options, store, subscription):
        self.options = options
        self.store = store
        self.subscription = subscription

    def update(self, notification):
        global QUEUE
        try:
            QUEUE
        except NameError:
            QUEUE = Queue()
            Processor(self.options).start()

        QUEUE.put((self.store, notification, self.subscription))

def _subscription_object(store, resource):
    # specific mail/contacts folder
    for (pattern, datatype) in (PATTERN_MESSAGES, PATTERN_CONTACTS, PATTERN_EVENTS):
        match = pattern.match('/'+resource)
        if match:
            return utils._folder(store, match.groupdict()['folderid']), None, datatype

    # all mail
    if resource == 'me/messages':
        return store.inbox, None, 'Message'

    # all contacts
    elif resource == 'me/contacts':
        return store.contacts, None, 'Contact'

    # all events
    elif resource in ('me/events', 'me/calendar/events'):
        return store.calendar, None, 'Event'

def _export_subscription(subscription):
    return dict((a,b) for (a,b) in subscription.items() if not a.startswith('_'))

class SubscriptionResource:
    def __init__(self, options):
        self.options = options

    def on_post(self, req, resp):
        user, store = _user_and_store(req, self.options)
        fields = json.loads(req.stream.read().decode('utf-8'))

        # validate webhook
        validationToken = str(uuid.uuid4())
        verify = not self.options or not self.options.insecure
        try: # TODO async
            logging.debug('validating subscription notification url: %s', fields['notificationUrl'])
            r = requests.post(fields['notificationUrl']+'?validationToken='+validationToken, timeout=10, verify=verify)
            if r.text != validationToken:
                logging.debug('subscription validation failed, validation token mismatch')
                raise utils.HTTPBadRequest("Subscription validation request failed.")
        except Exception:
            logging.exception('subscription validation request error')
            raise utils.HTTPBadRequest("Subscription validation request failed.")

        subscription_object = _subscription_object(store, fields['resource'])
        if not subscription_object:
            logging.error('subscription object is invalid')
            raise utils.HTTPBadRequest("Subscription object invalid.")
        target, folder_types, data_type = subscription_object

        # create subscription
        id_ = str(uuid.uuid4())
        subscription = fields
        subscription['id'] = id_
        subscription['_datatype'] = data_type

        sink = Sink(self.options, store, subscription)
        object_types = ['item'] # TODO folders not supported by graph atm?
        event_types = [x.strip() for x in subscription['changeType'].split(',')]

        target.subscribe(sink, object_types=object_types,
                         event_types=event_types, folder_types=folder_types)

        SUBSCRIPTIONS[id_] = (subscription, sink, user.userid)
        logging.debug('subscription created, id:%s, target:%s, object_types:%s, event_types:%s, folder_types:%s', id_, target, object_types, event_types, folder_types)

        resp.content_type = "application/json"
        if INDENT:
            resp.body = json.dumps(_export_subscription(subscription), indent=2, ensure_ascii=False).encode('utf-8')
        else:
            resp.body = json.dumps(_export_subscription(subscription), ensure_ascii=False).encode('utf-8')
        resp.status = falcon.HTTP_201

        if self.options and self.options.with_metrics:
            SUBSCR_COUNT.inc()
            SUBSCR_ACTIVE.set(len(SUBSCRIPTIONS))

    def on_get(self, req, resp, subscriptionid=None):
        user = _user(req, self.options)

        if subscriptionid:
            subscription, sink, userid = SUBSCRIPTIONS[subscriptionid]
            data = _export_subscription(subscription)
        else:
            userid = user.userid
            data = {
                '@odata.context': req.path,
                'value': [_export_subscription(subscription) for (subscription, _, uid) in SUBSCRIPTIONS.values() if uid == userid], # TODO doesn't scale
            }

        resp.content_type = "application/json"
        if INDENT:
            resp.body = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
        else:
            resp.body = json.dumps(data, ensure_ascii=False).encode('utf-8')

    def on_patch(self, req, resp, subscriptionid):
        user = _user(req, self.options)

        try:
            subscription, sink, userid = SUBSCRIPTIONS[subscriptionid]
        except KeyError:
            resp.status = falcon.HTTP_404
            return

        fields = json.loads(req.stream.read().decode('utf-8'))

        for k, v in fields.items():
            if v and k == 'expirationDateTime':
                # NOTE(longsleep): Setting a dict key which is already there is threadsafe in current CPython implementations.
                subscription['expirationDateTime'] = v

        #logging.debug('subscription updated, id:%s', subscriptionid)

        data = _export_subscription(subscription)

        resp.content_type = "application/json"
        if INDENT:
            resp.body = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
        else:
            resp.body = json.dumps(data, ensure_ascii=False).encode('utf-8')

    def on_delete(self, req, resp, subscriptionid):
        user, store = _user_and_store(req, self.options)

        try:
            subscription, sink, userid = SUBSCRIPTIONS[subscriptionid]
        except KeyError:
            resp.status = falcon.HTTP_404
            return

        store.unsubscribe(sink)
        del SUBSCRIPTIONS[subscriptionid]

        logging.debug('subscription deleted, id:%s', subscriptionid)

        if self.options and self.options.with_metrics:
            SUBSCR_ACTIVE.set(len(SUBSCRIPTIONS))

        resp.set_header('Content-Length', '0') # https://github.com/jonashaag/bjoern/issues/139
        resp.status = falcon.HTTP_204
