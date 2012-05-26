# Copyright 2011-2012 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

import os
import socket
import sys
import thread
import time
import threading
import weakref

from pymongo.errors import ConnectionFailure


have_ssl = True
try:
    import ssl
except ImportError:
    have_ssl = False

# mod_wsgi creates a synthetic mod_wsgi Python module; detect its version.
# See Pool._watch_current_thread for full explanation.
try:
    from mod_wsgi import version as mod_wsgi_version
except ImportError:
    mod_wsgi_version = None

# PyMongo does not use greenlet-aware connection pools by default, but it will
# attempt to do so if you pass use_greenlets=True to Connection or
# ReplicaSetConnection
have_greenlet = True
try:
    import greenlet
except ImportError:
    have_greenlet = False


NO_REQUEST    = None
NO_SOCKET_YET = -1


if sys.platform.startswith('java'):
    from select import cpython_compatible_select as select
else:
    from select import select


def _closed(sock):
    """Return True if we know socket has been closed, False otherwise.
    """
    try:
        rd, _, _ = select([sock], [], [], 0)
    # Any exception here is equally bad (select.error, ValueError, etc.).
    except:
        return True
    return len(rd) > 0


class SocketInfo(object):
    """Store a socket with some metadata
    """
    def __init__(self, sock, pool_id):
        self.sock = sock
        self.authset = set()
        self.closed = False
        self.last_checkout = time.time()

        # The pool's pool_id changes with each reset() so we can close sockets
        # created before the last reset.
        self.pool_id = pool_id

    def close(self):
        self.closed = True
        # Avoid exceptions on interpreter shutdown.
        try:
            self.sock.close()
        except:
            pass

    def __eq__(self, other):
        # Need to check if other is NO_REQUEST or NO_SOCKET_YET, and then check
        # if its sock is the same as ours
        return hasattr(other, 'sock') and self.sock == other.sock

    def __hash__(self):
        return hash(self.sock)

    def __repr__(self):
        return "SocketInfo(%s)%s at %s" % (
            repr(self.sock),
            self.closed and " CLOSED" or "",
            id(self)
        )


class BasePool(object):
    def __init__(self, pair, max_size, net_timeout, conn_timeout, use_ssl):
        """
        :Parameters:
          - `pair`: a (hostname, port) tuple
          - `max_size`: approximate number of idle connections to keep open
          - `net_timeout`: timeout in seconds for operations on open connection
          - `conn_timeout`: timeout in seconds for establishing connection
          - `use_ssl`: bool, if True use an encrypted connection
        """
        self.sockets = set()
        self.lock = threading.Lock()

        # Keep track of resets, so we notice sockets created before the most
        # recent reset and close them.
        self.pool_id = 0
        self.pid = os.getpid()
        self.pair = pair
        self.max_size = max_size
        self.net_timeout = net_timeout
        self.conn_timeout = conn_timeout
        self.use_ssl = use_ssl
        
        # Map self._get_thread_ident() -> request socket
        self._tid_to_sock = {}

        # Weakrefs used by subclasses to watch for dead threads or greenlets.
        # We must keep a reference to the weakref to keep it alive for at least
        # as long as what it references, otherwise its delete-callback won't
        # fire.
        self._refs = {}

    def reset(self):
        # Ignore this race condition -- if many threads are resetting at once,
        # the pool_id will definitely change, which is all we care about.
        self.pool_id += 1

        request_state = self._get_request_state()
        self.pid = os.getpid()

        # Close this thread's request socket. Other threads may be using their
        # request sockets right now, so don't close them. The next time each
        # thread tries to use its request socket, it will notice the changed
        # pool_id and close the socket.
        if request_state not in (NO_REQUEST, NO_SOCKET_YET):
            request_state.close()

        sockets = None
        try:
            # Swapping variables is not atomic. We need to ensure no other
            # thread is modifying self.sockets, or replacing it, in this
            # critical section.
            self.lock.acquire()
            sockets, self.sockets = self.sockets, set()
        finally:
            self.lock.release()

        for sock_info in sockets: sock_info.close()

        # If we were in a request before the reset, then delete the request
        # socket, but resume the request with a new socket the next time
        # get_socket() is called.
        if request_state != NO_REQUEST:
            self._set_request_state(NO_SOCKET_YET)

    def create_connection(self, pair):
        """Connect to *pair* and return the socket object.

        This is a modified version of create_connection from
        CPython >=2.6.
        """
        host, port = pair or self.pair

        # Don't try IPv6 if we don't support it. Also skip it if host
        # is 'localhost' (::1 is fine). Avoids slow connect issues
        # like PYTHON-356.
        family = socket.AF_INET
        if socket.has_ipv6 and host != 'localhost':
            family = socket.AF_UNSPEC

        err = None
        for res in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
            af, socktype, proto, dummy, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self.conn_timeout or 20.0)
                sock.connect(sa)
                return sock
            except socket.error, e:
                err = e
                if sock is not None:
                    sock.close()

        if err is not None:
            raise err
        else:
            # This likely means we tried to connect to an IPv6 only
            # host with an OS/kernel or Python interpeter that doesn't
            # support IPv6. The test case is Jython2.5.1 which doesn't
            # support IPv6 at all.
            raise socket.error('getaddrinfo failed')

    def connect(self, pair):
        """Connect to Mongo and return a new (connected) socket. Note that the
           pool does not keep a reference to the socket -- you must call
           return_socket() when you're done with it.
        """
        sock = self.create_connection(pair)

        if self.use_ssl:
            try:
                sock = ssl.wrap_socket(sock)
            except ssl.SSLError:
                sock.close()
                raise ConnectionFailure("SSL handshake failed. MongoDB may "
                                        "not be configured with SSL support.")

        sock.settimeout(self.net_timeout)
        return SocketInfo(sock, self.pool_id)

    def get_socket(self, pair=None):
        """Get a socket from the pool.

        Returns a :class:`SocketInfo` object wrapping a connected
        :class:`socket.socket`, and a bool saying whether the socket was from
        the pool or freshly created.

        :Parameters:
          - `pair`: optional (hostname, port) tuple
        """
        # We use the pid here to avoid issues with fork / multiprocessing.
        # See test.test_connection:TestConnection.test_fork for an example of
        # what could go wrong otherwise
        if self.pid != os.getpid():
            self.reset()

        # Have we opened a socket for this request?
        req_state = self._get_request_state()
        if req_state not in (NO_SOCKET_YET, NO_REQUEST):
            # There's a socket for this request, check it and return it
            checked_sock = self._check(req_state, pair)
            if checked_sock != req_state:
                self._set_request_state(checked_sock)

            checked_sock.last_checkout = time.time()
            return checked_sock

        # We're not in a request, just get any free socket or create one
        sock_info, from_pool = None, None
        try:
            try:
                # set.pop() isn't atomic in Jython less than 2.7, see
                # http://bugs.jython.org/issue1854
                self.lock.acquire()
                sock_info, from_pool = self.sockets.pop(), True
            finally:
                self.lock.release()
        except KeyError:
            sock_info, from_pool = self.connect(pair), False

        if from_pool:
            sock_info = self._check(sock_info, pair)

        if req_state == NO_SOCKET_YET:
            # start_request has been called but we haven't assigned a socket to
            # the request yet. Let's use this socket for this request until
            # end_request.
            self._set_request_state(sock_info)

        sock_info.last_checkout = time.time()
        return sock_info

    def start_request(self):
        if self._get_request_state() == NO_REQUEST:
            # Add a placeholder value so we know we're in a request, but we
            # have no socket assigned to the request yet.
            self._set_request_state(NO_SOCKET_YET)

    def in_request(self):
        return self._get_request_state() != NO_REQUEST

    def end_request(self):
        sock_info = self._get_request_state()
        self._set_request_state(NO_REQUEST)
        if sock_info not in (NO_REQUEST, NO_SOCKET_YET):
            self._return_socket(sock_info)

    def maybe_return_socket(self, sock_info):
        """Return the socket to the pool unless it's the request socket.
        """
        if self.pid != os.getpid():
            self.reset()
        elif sock_info not in (NO_REQUEST, NO_SOCKET_YET):
            if sock_info.closed:
                return

            if sock_info != self._get_request_state():
                self._return_socket(sock_info)

    def _return_socket(self, sock_info):
        """Return socket to the pool. If pool is full the socket is discarded.
        """
        try:
            self.lock.acquire()
            if len(self.sockets) < self.max_size:
                self.sockets.add(sock_info)
            else:
                sock_info.close()
        finally:
            self.lock.release()

    def _check(self, sock_info, pair):
        """This side-effecty function checks if this pool has been reset since
        the last time this socket was used, or if the socket has been closed by
        some external network error, and if so, attempts to create a new socket.
        If this connection attempt fails we reset the pool and reraise the
        error.

        Checking sockets lets us avoid seeing *some*
        :class:`~pymongo.errors.AutoReconnect` exceptions on server
        hiccups, etc. We only do this if it's been > 1 second since
        the last socket checkout, to keep performance reasonable - we
        can't avoid AutoReconnects completely anyway.
        """
        error = False

        if self.pool_id != sock_info.pool_id:
            sock_info.close()
            error = True

        elif time.time() - sock_info.last_checkout > 1:
            if _closed(sock_info.sock):
                sock_info.close()
                error = True

        if not error:
            return sock_info
        else:
            try:
                return self.connect(pair)
            except socket.error:
                self.reset()
                raise

    def _set_request_state(self, sock_info):
        tid = self._get_thread_ident()

        if sock_info == NO_REQUEST:
            # Ending a request
            self._refs.pop(tid, None)
            self._tid_to_sock.pop(tid, None)
        else:
            self._tid_to_sock[tid] = sock_info

            if tid not in self._refs:
                # Closure over tid.
                # Do not access threadlocals in this function, or any
                # function it calls! In the case of the Pool subclass and
                # mod_wsgi 2.x, on_thread_died() is triggered when mod_wsgi
                # calls PyThreadState_Clear(), which deferences the
                # ThreadVigil and triggers the weakref callback. Accessing
                # thread locals in this function, while PyThreadState_Clear()
                # is in progress can cause leaks, see PYTHON-353.
                def on_thread_died(ref):
                    try:
                        # End the request
                        self._refs.pop(tid, None)
                        request_sock = self._tid_to_sock.pop(tid)
                    except:
                        # KeyError if dead thread wasn't in a request,
                        # or random exceptions on interpreter shutdown.
                        return

                    # Was thread ever really assigned a socket before it died?
                    if request_sock not in (NO_REQUEST, NO_SOCKET_YET):
                        self._return_socket(request_sock)

                self._watch_current_thread(on_thread_died)

    def _get_request_state(self):
        tid = self._get_thread_ident()
        return self._tid_to_sock.get(tid, NO_REQUEST)

    # Overridable methods for pools.
    def _get_thread_ident(self):
        raise NotImplementedError

    def _watch_current_thread(self, callback):
        raise NotImplementedError


class Pool(BasePool):
    """A simple connection pool.

    Calling start_request() acquires a thread-local socket, which is returned
    to the pool when the thread calls end_request() or dies.
    """
    def __init__(self, *args, **kwargs):
        super(Pool, self).__init__(*args, **kwargs)
        self._local = threading.local()

    # Overrides
    def _get_request_state(self):
        # In Python <= 2.6, a dead thread's locals aren't cleaned up until the
        # next access. That can lead to a nasty race where a new thread with
        # the same ident as a previous one does _get_request_state() and thinks
        # it's still in the previous thread's request. Only when some thread
        # next accesses self._local.vigil does the dead thread's vigil get
        # destroyed, triggered on_thread_died and returning the request socket
        # to self.sockets. At that point a different thread can acquire that
        # socket, and with two threads using the same socket they'll read
        # each other's data. A symptom is an AssertionError in
        # Connection.__receive_message_on_socket().

        # Accessing the thread local here guarantees that a previous thread's
        # locals are cleaned up before we check request state, and so even if
        # this thread has the same ident as a previous one, we don't think we're
        # in the same request.
        getattr(self._local, 'vigil', None)
        return super(Pool, self)._get_request_state()

    def _get_thread_ident(self):
        return thread.get_ident()

    # After a thread calls start_request() and we assign it a socket, we must
    # watch the thread to know if it dies without calling end_request so we can
    # return its socket to the idle pool, self.sockets. We watch for
    # thread-death using a weakref callback to a thread local. The weakref is
    # permitted on subclasses of object but not object() itself, so we make
    # this class.
    class ThreadVigil(object):
        pass

    def _watch_current_thread(self, callback):
        # In mod_wsgi 2.x, thread state is deleted between HTTP requests,
        # though the thread remains. This mismatch between thread locals and
        # threads can cause bugs in Pool, but since mod_wsgi threads always
        # last as long as the process, we don't have to watch for this thread's
        # death. See PYTHON-353.
        if mod_wsgi_version and mod_wsgi_version[0] <= 2:
            return

        tid = self._get_thread_ident()
        self._local.vigil = vigil = Pool.ThreadVigil()
        self._refs[tid] = weakref.ref(vigil, callback)


class GreenletPool(BasePool):
    """A simple connection pool.

    Calling start_request() acquires a greenlet-local socket, which is returned
    to the pool when the greenlet calls end_request() or dies.
    """
    # Overrides
    def _get_thread_ident(self):
        return id(greenlet.getcurrent())

    def _watch_current_thread(self, callback):
        current = greenlet.getcurrent()
        tid = self._get_thread_ident()

        if hasattr(current, 'link'):
            # This is a Gevent Greenlet (capital G), which inherits from
            # greenlet and provides a 'link' method to detect when the
            # Greenlet exits.
            current.link(callback)
            self._refs[tid] = None
        else:
            # This is a non-Gevent greenlet (small g), or it's the main
            # greenlet.
            self._refs[tid] = weakref.ref(current, callback)


class Request(object):
    """
    A context manager returned by Connection.start_request(), so you can do
    `with connection.start_request(): do_something()` in Python 2.5+.
    """
    def __init__(self, connection):
        self.connection = connection

    def end(self):
        self.connection.end_request()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end()
        # Returning False means, "Don't suppress exceptions if any were
        # thrown within the block"
        return False
