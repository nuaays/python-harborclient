"""
Harbor Client interface. Handles the REST calls and responses.
"""

import copy
import hashlib
import logging
import warnings

from oslo_utils import importutils
import requests

try:
    import json
except ImportError:
    import simplejson as json

from harborclient import api_versions
from harborclient import exceptions
from harborclient.i18n import _
from harborclient.i18n import _LW
from harborclient import utils


class HTTPClient(object):
    USER_AGENT = 'python-harborclient'

    def __init__(self,
                 username,
                 password,
                 baseurl,
                 timeout=None,
                 timings=False,
                 http_log_debug=False,
                 api_version=None):
        self.username = username
        self.password = password
        self.baseurl = baseurl
        self.api_version = api_version or api_versions.APIVersion()
        self.timings = timings
        self.http_log_debug = http_log_debug
        if timeout is not None:
            self.timeout = float(timeout)
        else:
            self.timeout = None

        self.times = []  # [("item", starttime, endtime), ...]

        self._logger = logging.getLogger(__name__)
        self.session_id = None

        if self.http_log_debug and not self._logger.handlers:
            # Logging level is already set on the root logger
            ch = logging.StreamHandler()
            self._logger.addHandler(ch)
            self._logger.propagate = False
            if hasattr(requests, 'logging'):
                rql = requests.logging.getLogger(requests.__name__)
                rql.addHandler(ch)
                # Since we have already setup the root logger on debug, we
                # have to set it up here on WARNING (its original level)
                # otherwise we will get all the requests logging messages
                rql.setLevel(logging.WARNING)

    def unauthenticate(self):
        """Forget all of our authentication information."""
        requests.get(
            '%s://%s/logout' % (self.protocol, self.host),
            cookies={'beegosessionID': self.session_id})
        logging.debug("Successfully logout")

    def get_timings(self):
        return self.times

    def reset_timings(self):
        self.times = []

    def _redact(self, target, path, text=None):
        """Replace the value of a key in `target`.

        The key can be at the top level by specifying a list with a single
        key as the path. Nested dictionaries are also supported by passing a
        list of keys to be navigated to find the one that should be replaced.
        In this case the last one is the one that will be replaced.

        :param dict target: the dictionary that may have a key to be redacted;
                            modified in place
        :param list path: a list representing the nested structure in `target`
                          that should be redacted; modified in place
        :param string text: optional text to use as a replacement for the
                            redacted key. if text is not specified, the
                            default text will be sha1 hash of the value being
                            redacted
        """

        key = path.pop()

        # move to the most nested dict
        for p in path:
            try:
                target = target[p]
            except KeyError:
                return

        if key in target:
            if text:
                target[key] = text
            elif target[key] is not None:
                # because in python3 byte string handling is ... ug
                value = target[key].encode('utf-8')
                sha1sum = hashlib.sha1(value)
                target[key] = "{SHA1}%s" % sha1sum.hexdigest()

    def http_log_req(self, method, url, kwargs):
        if not self.http_log_debug:
            return

        string_parts = ['curl -g -i']

        if not kwargs.get('verify', True):
            string_parts.append(' --insecure')

        string_parts.append(" '%s'" % url)
        string_parts.append(' -X %s' % method)

        headers = copy.deepcopy(kwargs['headers'])
        # because dict ordering changes from 2 to 3
        keys = sorted(headers.keys())
        for name in keys:
            value = headers[name]
            header = ' -H "%s: %s"' % (name, value)
            string_parts.append(header)
        cookies = kwargs['cookies']
        for name in sorted(cookies.keys()):
            value = cookies[name]
            cookie = header = ' -b "%s: %s"' % (name, value)
            string_parts.append(cookie)
        if 'data' in kwargs:
            data = json.loads(kwargs['data'])
            string_parts.append(" -d '%s'" % json.dumps(data))
        self._logger.debug("REQ: %s" % "".join(string_parts))

    def http_log_resp(self, resp):
        if not self.http_log_debug:
            return

        if resp.text and resp.status_code != 400:
            try:
                body = json.loads(resp.text)
            except ValueError:
                body = None
        else:
            body = None

        self._logger.debug("RESP: [%(status)s] %(headers)s\nRESP BODY: "
                           "%(text)s\n", {
                               'status': resp.status_code,
                               'headers': resp.headers,
                               'text': json.dumps(body)
                           })

    def request(self, url, method, **kwargs):
        url = self.baseurl + "/api" + url
        kwargs.setdefault('headers', kwargs.get('headers', {}))
        kwargs['headers']['User-Agent'] = self.USER_AGENT
        kwargs['headers']['Accept'] = 'application/json'
        if 'body' in kwargs:
            kwargs['headers']['Content-Type'] = 'application/json'
            kwargs['data'] = json.dumps(kwargs['body'])
            del kwargs['body']
        api_versions.update_headers(kwargs["headers"], self.api_version)
        if self.timeout is not None:
            kwargs.setdefault('timeout', self.timeout)

        self.http_log_req(method, url, kwargs)

        resp = requests.request(method, url, **kwargs)
        self.http_log_resp(resp)

        if resp.text:
            # TODO(dtroyer): verify the note below in a requests context
            # NOTE(alaski): Because force_exceptions_to_status_code=True
            # httplib2 returns a connection refused event as a 400 response.
            # To determine if it is a bad request or refused connection we need
            # to check the body.  httplib2 tests check for 'Connection refused'
            # or 'actively refused' in the body, so that's what we'll do.
            if resp.status_code == 400:
                if ('Connection refused' in resp.text or
                        'actively refused' in resp.text):
                    raise exceptions.ConnectionRefused(resp.text)
            try:
                body = json.loads(resp.text)
            except ValueError:
                body = None
        else:
            body = None

        self.last_request_id = (resp.headers.get('x-harbor-request-id')
                                if resp.headers else None)
        if resp.status_code >= 400:
            raise exceptions.from_response(resp, body, url, method)

        return resp, body

    def _time_request(self, url, method, **kwargs):
        with utils.record_time(self.times, self.timings, method, url):
            resp, body = self.request(url, method, **kwargs)
        return resp, body

    def _cs_request(self, url, method, **kwargs):
        if not self.session_id:
            self.authenticate()
        # Perform the request once. If we get a 401 back then it
        # might be because the auth token expired, so try to
        # re-authenticate and try again. If it still fails, bail.
        try:
            resp, body = self._time_request(
                url,
                method,
                cookies={'beegosessionID': self.session_id},
                **kwargs)
            return resp, body
        except exceptions.Unauthorized as e:
            try:
                # first discard auth token, to avoid the possibly expired
                # token being re-used in the re-authentication attempt
                self.unauthenticate()
                # overwrite bad token
                self.authenticate()
                resp, body = self._time_request(url, method, **kwargs)
                return resp, body
            except exceptions.Unauthorized:
                raise e

    def get(self, url, **kwargs):
        return self._cs_request(url, 'GET', **kwargs)

    def post(self, url, **kwargs):
        return self._cs_request(url, 'POST', **kwargs)

    def put(self, url, **kwargs):
        return self._cs_request(url, 'PUT', **kwargs)

    def delete(self, url, **kwargs):
        return self._cs_request(url, 'DELETE', **kwargs)

    def authenticate(self):
        if not self.baseurl:
            msg = _("Authentication requires 'baseurl', which should be "
                    "specified in '%s'") % self.__class__.__name__
            raise exceptions.AuthorizationFailure(msg)

        if not self.username:
            msg = _("Authentication requires 'username', which should be "
                    "specified in '%s'") % self.__class__.__name__
            raise exceptions.AuthorizationFailure(msg)

        if not self.password:
            msg = _("Authentication requires 'password', which should be "
                    "specified in '%s'") % self.__class__.__name__
            raise exceptions.AuthorizationFailure(msg)

        resp = requests.post(
            self.baseurl + "/login",
            data={'principal': self.username,
                  'password': self.password})
        if resp.status_code == 200:
            self.session_id = resp.cookies.get('beegosessionID')
            logging.debug(
                "Successfully login, session id: %s" % self.session_id)


def _construct_http_client(username=None,
                           password=None,
                           baseurl=None,
                           timeout=None,
                           extensions=None,
                           timings=False,
                           http_log_debug=False,
                           user_agent='python-harborclient',
                           api_version=None,
                           **kwargs):
    return HTTPClient(
        username,
        password,
        baseurl,
        timeout=timeout,
        timings=timings,
        http_log_debug=http_log_debug,
        api_version=api_version)


def _get_client_class_and_version(version):
    if not isinstance(version, api_versions.APIVersion):
        version = api_versions.get_api_version(version)
    else:
        api_versions.check_major_version(version)
    if version.is_latest():
        raise exceptions.UnsupportedVersion(
            _("The version should be explicit, not latest."))
    return version, importutils.import_class(
        "harborclient.v%s.client.Client" % version.ver_major)


def get_client_class(version):
    """Returns Client class based on given version."""
    warnings.warn(
        _LW("'get_client_class' is deprecated. "
            "Please use `harborclient.client.Client` instead."))
    _api_version, client_class = _get_client_class_and_version(version)
    return client_class


def Client(version,
           username=None,
           password=None,
           baseurl=None,
           *args,
           **kwargs):
    """Initialize client object based on given version.

    HOW-TO:
    The simplest way to create a client instance is initialization with your
    credentials::

        >>> from harborclient import client
        >>> harbor = client.Client(VERSION, USERNAME, PASSWORD,
        ...                      PROJECT_ID, AUTH_URL)

    Here ``VERSION`` can be a string or
    ``harborclient.api_versions.APIVersion`` obj. If you prefer string value,
    you can use ``1.1`` (deprecated now), ``2`` or ``2.X``
    (where X is a microversion).


    Alternatively, you can create a client instance using the keystoneauth
    session API. See "The harborclient Python API" page at
    python-harborclient's doc.
    """
    if args:
        warnings.warn("Only VERSION, USERNAME, PASSWORD, PROJECT_ID and "
                      "AUTH_URL arguments can be specified as positional "
                      "arguments. All other variables should be keyword "
                      "arguments. Note that this will become an error in "
                      "Ocata.")
    api_version, client_class = _get_client_class_and_version(version)
    kwargs.pop("direct_use", None)
    return client_class(
        username=username,
        password=password,
        baseurl=baseurl,
        api_version=api_version,
        *args,
        **kwargs)
