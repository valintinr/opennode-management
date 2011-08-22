import os

from twisted.internet import defer
from twisted.python import log

from opennode.oms.endpoint.ssh import cmdline
from opennode.oms.endpoint.ssh.cmd import registry, completion
from opennode.oms.endpoint.ssh.colored_columnize import columnize
from opennode.oms.endpoint.ssh.terminal import InteractiveTerminal, BLUE, CYAN
from opennode.oms.endpoint.ssh.tokenizer import CommandLineTokenizer, CommandLineSyntaxError
from opennode.oms.zodb import db


class OmsSshProtocol(InteractiveTerminal):
    """The OMS virtual console over SSH.

    Accepts lines of input and writes them back to its connection.  If
    a line consisting solely of "quit" is received, the connection
    is dropped.

    """

    def __init__(self):
        super(OmsSshProtocol, self).__init__()
        self.path = ['']
        self.last_error = None

        @defer.inlineCallbacks
        def _get_obj_path():
            # Here, we simply hope that self.obj_path won't actually be
            # used until it's initialised.  A more fool-proof solution
            # would be to block everything in the protocol while the ZODB
            # query is processing, but that would require a more complex
            # workaround.  This will not be a problem during testing as
            # DB access is blocking when testing.
            self.obj_path = yield db.transact(lambda: [db.ref(db.get_root()['oms_root'])])()

        _get_obj_path()

        self.tokenizer = CommandLineTokenizer()

    def lineReceived(self, line):
        line = line.strip()

        try:
            command, cmd_args = self.parse_line(line)
        except CommandLineSyntaxError as e:
            self.terminal.write("Syntax error: %s\n" % (e.message))
            self.print_prompt()
            return

        deferred = defer.maybeDeferred(command, *cmd_args)

        @deferred
        def on_error(f):
            if not f.check(cmdline.ArgumentParsingError):
                self.terminal.write("Command returned an unhandled error: %s\n" % f.getErrorMessage())
                self.last_error = (line, f)
                log.msg("Got exception executing '%s': %s" % self.last_error)
                self.terminal.write("type last_error for more details\n")

        deferred.addBoth(lambda *_: self.print_prompt())

        ret = defer.Deferred()
        deferred.addBoth(ret.callback)
        return ret

    def parse_line(self, line):
        """Returns a command instance and parsed cmdline argument list.

        TODO: Shell expansion should be handled here.

        """

        cmd_name, cmd_args = line.partition(' ')[::2]
        command_cls = registry.get_command(cmd_name)

        tokenized_cmd_args = self.tokenizer.tokenize(cmd_args.strip())

        return command_cls(self), tokenized_cmd_args

    @defer.inlineCallbacks
    def handle_TAB(self):
        """Handles tab completion."""
        partial, rest, completions = yield completion.complete(self, self.lineBuffer, self.lineBufferIndex)

        if len(completions) == 1:
            space = '' if rest else ' '
            # handle quote closing
            if self.lineBuffer[self.lineBufferIndex - len(partial) - 1] == '"':
                space = '" '
            # Avoid space after '=' just for aestetics.
            # Avoid space after '/' for functionality.
            for i in ('=', '/'):
                if completions[0].endswith(i):
                    space = ''

            patch = completions[0][len(partial):] + space

            # Drop @, half hack
            if patch.endswith('@ '):
                patch = patch.rstrip('@ ') + ' '

            self.insert_text(patch)
        elif len(completions) > 1:
            common_prefix = os.path.commonprefix(completions)
            patch = common_prefix[len(partial):]
            self.insert_text(patch)

            # postpone showing list of possible completions until next tab
            if not patch:
                self.terminal.nextLine()
                completions = [self.colorize(self._completion_color(item), item) for item in completions]
                self.terminal.write(columnize(completions, self.width))
                self.drawInputLine()
                if len(rest):
                    self.terminal.cursorBackward(len(rest))

    def _completion_color(self, completion):
        if completion.endswith('/'):
            return BLUE
        elif completion.endswith('@'):
            return CYAN
        else:
            return None

    @property
    def hist_file_name(self):
        return os.path.expanduser('~/.oms_history')

    @property
    def ps(self):
        ps1 = '%s@%s:%s%s ' % ('user', 'oms', self._cwd(), '#')
        return [ps1, '... ']

    def _cwd(self):
        return self.make_path(self.path)

    @staticmethod
    def make_path(path):
        return '/'.join(path) or '/'
