import json
import time
import uuid

from twisted.internet import defer, reactor
from twisted.web import resource
from twisted.web.server import NOT_DONE_YET
from twisted.conch.insults.insults import ServerProtocol


from opennode.oms.endpoint.ssh.protocol import OmsSshProtocol

class WebTransport(object):
    """Used by WebTerminal to actually send the data through the http transport."""

    def __init__(self, session):
        self.session = session

    def write(self, text):
        # Group together writes so that we reduce the number of http roundtrips.
        self.session.buffer += text
        reactor.callLater(0.05, self.session.processQueue)


class WebTerminal(ServerProtocol):
    """Used by OmsSshProtocol to actually manipulate the terminal."""

    def __init__(self, session):
        ServerProtocol.__init__(self)
        self.session = session
        self.transport = WebTransport(session)
        self.terminalProtocol = session.shell


class TerminalSession(object):
    """A session for our ajax terminal emulator."""

    def __init__(self):
        self.id = str(uuid.uuid4())
        self.queue = []
        self.buffer = ""

        # TODO: handle session timeouts
        self.timestamp = time.time()

        # We can reuse the OmsSshProtocol without any change
        self.shell = OmsSshProtocol()
        self.shell.terminal = WebTerminal(self)
        self.shell.connectionMade()

    def parse_keys(self, key_stream):
        """The ajax protocol encodes keystrokes as a string of hex bytes,
        so each char code occupies to characters in the encoded form."""
        while key_stream:
            yield chr(int(key_stream[0:2], 16))
            key_stream = key_stream[2:]

    def handle_keys(self, key_stream):
        """Send each input key the terminal."""
        for key in self.parse_keys(key_stream):
            self.shell.terminal.dataReceived(key)

    def processQueue(self):
        # Teoretically only one ongoing polling request should be live.
        # but I'm not sure if this can be guaranteed so let's keep them all.
        if self.queue:
            for r in self.queue:
                self.write(r)
            self.queue = []

    def write(self, request):
        # chunk writes because the javascript renderer is very slow
        # this avoids long pauses to the user.
        chunk_size = 100
        chunk = self.buffer[0:chunk_size]

        request.write(json.dumps(dict(session=self.id, data=chunk)))
        request.finish()

        self.buffer = self.buffer[chunk_size:]

    def __repr__(self):
        return 'TerminalSession(%s, %s, %s, %s)' % (self.id, self.queue, self.buffer, self.timestamp)


class ManagementTerminalServer(resource.Resource):
    """Creates new OMS management console web sessions."""

    def render_OPTIONS(self, request):
        """Return headers which allow cross domain xhr for this."""
        headers = request.responseHeaders
        headers.addRawHeader('Access-Control-Allow-Origin', '*')
        headers.addRawHeader('Access-Control-Allow-Methods', 'POST, OPTIONS')
        # this is necessary for firefox
        headers.addRawHeader('Access-Control-Allow-Headers', 'Origin, Content-Type, Cache-Control')
        # this is to adhere to the OPTIONS method, not necessary for cross-domain
        headers.addRawHeader('Allow', 'GET, POST, OPTIONS')

        return ""

    def __init__(self, avatar=None):
        # Twisted Resource is a not a new style class, so emulating a super-call.
        resource.Resource.__init__(self)

        self.sessions = {}

    def render_POST(self, request):
        # Allow for cross-domain, at least for testing.
        request.responseHeaders.addRawHeader('Access-Control-Allow-Origin', '*')

        session_id = request.args.get('session', [None])[0]

        # The handshake consists of the session id and initial data to be rendered.
        if not session_id:
            session = TerminalSession()
            self.sessions[session.id] = session
            return json.dumps(dict(session=session.id, data=session.buffer))

        session = self.sessions[session_id]

        # There are two types of requests:
        # 1) user type keystrokes, return synchronously
        # 2) long polling requests are suspended until there is activity from the terminal
        keys = request.args.get('keys', None)
        if keys:
            session.handle_keys(keys[0])
            return ""
        else:
            session.queue.append(request)
            if session.buffer:
                defer.maybeDeferred(session.processQueue)

        return NOT_DONE_YET


class WebTerminalServer(resource.Resource):
    """ShellInABox web terminal protocol handler."""

    isLeaf = False

    def getChild(self, name, request):
        """For now the only mounted terminal service is the commadnline oms management.
        We'll mount here the ssh consoles to machines."""
        if name == 'management':
            return self.management
        return self

    def __init__(self, avatar=None):
        # Twisted Resource is a not a new style class, so emulating a super-call.
        resource.Resource.__init__(self)
        self.avatar = avatar

        self.management = ManagementTerminalServer()

    def render(self, request):
        return ""
