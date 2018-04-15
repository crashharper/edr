from sseclient import SSEClient
from Queue import Queue
import requests
import json
import threading
import socket
import sys

import edtime
from edrlog import EDRLog

class EDRRealtimeUpdates(object):
    def __init__(self, callback, kind, endpoint, authenticator=None):
        self.endpoint = endpoint
        self.authenticator = authenticator
        self.inbound_queue = Queue()
        self.remote_thread = RemoteThread(self.inbound_queue, endpoint, authenticator)
        self.disp = EDRSEEReader(self.inbound_queue, callback, kind)

    def update_auth(self, authenticator):
        self.authenticator = authenticator
        self.remote_thread.update_auth(authenticator)

    def reset(self):
        self.inbound_queue.queue.clear()
        self.remote_thread = RemoteThread(self.inbound_queue, self.endpoint, self.authenticator)

    def start(self):
        if self.remote_thread.is_alive():
            return
        
        self.inbound_queue.queue.clear()
        self.remote_thread.start()

    def shutdown(self):
        self.remote_thread.close()
        self.remote_thread.join()

class ClosableSSEClient(SSEClient):
    """
    Hack in some closing functionality on top of the SSEClient
    """

    def __init__(self, *args, **kwargs):
        self.should_connect = True
        super(ClosableSSEClient, self).__init__(*args, **kwargs)

    def _connect(self):
        if self.should_connect:
            super(ClosableSSEClient, self)._connect()
        else:
            raise StopIteration()

    def close(self):
        self.should_connect = False
        self.retry = 0
        # HACK: dig through the sseclient library to the requests library down to the underlying socket.
        # then close that to raise an exception to get out of streaming. I should probably file an issue w/ the
        # requests library to make this easier
        self.resp.raw._fp.fp._sock.shutdown(socket.SHUT_RDWR)
        self.resp.raw._fp.fp._sock.close()

class RemoteThread(threading.Thread):

    def __init__(self, message_queue, endpoint, authenticator, minutes_ago=10):
        self.message_queue = message_queue
        self.endpoint = endpoint
        self.sse = None
        self.authenticator = authenticator
        self.minutes_ago = minutes_ago
        super(RemoteThread, self).__init__()

    def update_auth(self, authenticator):
        self.authenticator = authenticator

    def setup_sse(self):
        if self.sse:
            self.sse.close()
            self.sse = None
        nowish = edtime.EDTime.js_epoch_now() - 1000*60*self.minutes_ago
        params = { "orderBy": '"timestamp"', "startAt": nowish}
        if self.authenticator:
            params = { "orderBy": '"timestamp"', "startAt": nowish , "auth": self.authenticator()}
        self.sse = ClosableSSEClient(self.endpoint, params=params, chunk_size=1)
    
    def run(self):
        self.setup_sse()
        edr_log = EDRLog()
        try:
            for msg in self.sse:
                if msg.event is "keep-alive":
                    edr_log.log(u"SSE keep-alive received", "DEBUG")
                    continue
                if msg.event is "auth_revoked":
                    edr_log.log(u"SSE auth_revoked received", "DEBUG")
                    self.setup_sse()
                    continue
                if msg.event is "cancel":
                    edr_log.log(u"SSE cancel received", "DEBUG")
                    #TODO we lost permission to read at the endpoint
                    break
                edr_log.log(u"SSE msg received: {} {}".format(msg.event, msg.data), "DEBUG")
                self.message_queue.put(msg)
        except socket.error:
            pass    # this can happen when we close the stream
        except requests.HTTPError:
            pass    # this can happen when the auth is no longer valid

    def close(self):
        if self.sse:
            self.sse.close()


class EDRSEEReader():

    class EDRSEEThread(threading.Thread):

        def __init__(self, inbound_queue, callback, kind):
            self.inbound_queue = inbound_queue
            self.callback = callback
            self.kind = kind
            super(EDRSEEReader.EDRSEEThread, self).__init__()

        def run(self):
            edr_log = EDRLog()
            while True:
                msg = self.inbound_queue.get()
                if not msg:
                    edr_log.log(u"SSE stop signal received.", "DEBUG")
                    break
                edr_log.log(u"handling msg: {} {} {}".format(msg.event, msg.data, self.kind), "DEBUG")
                if msg.event in ["put", "patch"] and msg.data:
                    self.callback(self.kind, json.loads(msg.data)["data"])
                # TODO handle event = put when path is not "/", initial state. or maybe it's fine / different in that case given the timestamp orderby
                # TODO handle event = patch

    def __init__(self, inbound_queue, callback, kind):
        self.inbound_queue = inbound_queue
        self.see_thread = self.setup_see_thread(callback, kind)
        self.see_thread.start()

    def setup_see_thread(self, callback, kind):
        return self.EDRSEEThread(self.inbound_queue, callback, kind)

    def close(self):
        self.inbound_queue.put(False)
        self.see_thread.join()
