from __future__ import absolute_import

from grokcore.component import context, baseclass
from twisted.internet import defer
from zope import schema
from zope.component import provideSubscriptionAdapter
from zope.interface import Interface, implements

from .actions import ActionsContainerExtension, Action, action
from .base import Container, ReadonlyContainer
from opennode.oms.endpoint.webterm.ssh import ssh_connect_interactive_shell
from opennode.oms.endpoint.ssh.terminal import RESET_COLOR


class IConsole(Interface):
    """Console node."""


class ITextualConsole(Interface):
    """Textual console."""


class IGraphicalConsole(Interface):
    """Graphical console."""


class ITtyConsole(IConsole):
    pty = schema.TextLine(title=u"pty")


class ISshConsole(IConsole):
    user = schema.TextLine(title=u"user")
    hostname = schema.TextLine(title=u"hostname")
    port = schema.Int(title=u"port")


class IVncConsole(IConsole):
    hostname = schema.TextLine(title=u"hostname")
    port = schema.Int(title=u"port")


class TtyConsole(ReadonlyContainer):
    implements(ITtyConsole, ITextualConsole)


class TtyConsole(ReadonlyContainer):
    implements(ITtyConsole, ITextualConsole)

    def __init__(self, name, pty):
        self.__name__ = name
        self.pty = pty


class SshConsole(ReadonlyContainer):
    implements(ISshConsole, ITextualConsole)

    def __init__(self, name, user, hostname, port):
        self.__name__ = name
        self.user = user
        self.hostname = hostname
        self.port = port


class VncConsole(ReadonlyContainer):
    implements(IVncConsole, IGraphicalConsole)

    def __init__(self, hostname, port):
        self.__name__ = 'vnc'
        self.hostname = hostname
        self.port = port


class Consoles(Container):
    __name__ = 'consoles'


class AttachAction(Action):
    """Attach to textual console"""
    baseclass()

    action('attach')

    def execute(self, cmd, args):
        self.closed = False
        self.protocol = cmd.protocol
        self.transport = self
        size = (cmd.protocol.width, cmd.protocol.height)

        self._do_connection(size)

        self.deferred = defer.Deferred()
        return self.deferred

    def write(self, data):
        if not self.closed:
            self.protocol.terminal.write(data)

    def loseConnection(self):
        self.closed = True
        self.protocol.terminal.resetPrivateModes('1')
        self.protocol.terminal.write(RESET_COLOR)
        self.deferred.callback(None)

    def _set_channel(self, channel):
        loseConnection = self.loseConnection

        class SshSubProtocol(object):
            def __init__(self, parent):
                self.parent = parent
                self.buffer = []

            def dataReceived(self, data):
                for ch in data:
                    if ch == '\x1d':
                        # TODO: really close the ssh connection
                        loseConnection()
                channel.write(data)

        self.protocol.sub_protocol = SshSubProtocol(self.protocol)


class SshAttachAction(AttachAction):
    context(ISshConsole)

    def _do_connection(self, size):
        self.write("Attaching to %s@%s. Use ^] to force exit.\n" % (self.context.user.encode('utf-8'), self.context.hostname.encode('utf-8')))

        ssh_connect_interactive_shell(self.context.user, self.context.hostname, self.context.port, self.transport, self._set_channel, size)


class TtyAttachAction(AttachAction):
    context(ITtyConsole)

    def _do_connection(self, size):
        self.write("Attaching to %s. Use ^] to force exit.\n" % (self.context.pty.encode('utf-8')))

        command = 'screen -xRR %s %s' % (self.context.pty.replace('/',''), self.context.pty)
        phy = self.context.__parent__.__parent__.__parent__.__parent__

        ssh_connect_interactive_shell('root', phy.hostname, 22, self.transport, self._set_channel, size, command)


provideSubscriptionAdapter(ActionsContainerExtension, adapts=(IConsole, ))