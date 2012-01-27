import collections
import transaction

from grokcore.component import implements
from zope.authentication.interfaces import IAuthentication
from zope.component import getUtility
from zope.securitypolicy.interfaces import IPrincipalRoleManager
from zope.securitypolicy.rolepermission import rolePermissionManager
from zope.securitypolicy.principalrole import principalRoleManager as prinroleG

from opennode.oms.endpoint.ssh.cmd.base import Cmd
from opennode.oms.endpoint.ssh.cmd.directives import command
from opennode.oms.endpoint.ssh.cmdline import ICmdArgumentsSyntax, VirtualConsoleArgumentParser, MergeListAction
from opennode.oms.security.checker import proxy_factory
from opennode.oms.security.permissions import Role
from opennode.oms.security.principals import User, Group, effective_principals
from opennode.oms.zodb import db


class NoSuchPermission(Exception):
    pass


class WhoAmICmd(Cmd):
    command('whoami')

    def execute(self, args):
        self.write("%s\n" % self.protocol.principal.id)


def effective_perms(interaction, obj):
    def roles_for(obj):
        prinrole = IPrincipalRoleManager(obj)

        allowed = {}

        def accumulate_roles(role_manager):
            for g in effective_principals(interaction):
                for role, setting in role_manager.getRolesForPrincipal(g.id):
                    allowed[role] = setting.getName() == 'Allow'

        accumulate_roles(prinroleG)
        accumulate_roles(prinrole)

        return allowed

    def parents(o):
        while o:
            yield o
            o = o.__parent__

    effective_allowed = {}
    with interaction:
        for p in reversed(list(parents(obj))):
            effective_allowed.update(roles_for(p))

    return [k for k, v in effective_allowed.items() if v]


def pretty_effective_perms(interaction, obj):
    perms = effective_perms(interaction, obj)
    return ''.join(i if Role.nick_to_role[i].id in perms else '-' for i in sorted(Role.nick_to_role.keys()))


class PermCheckCmd(Cmd):
    implements(ICmdArgumentsSyntax)

    command('permcheck')

    def arguments(self):
        parser = VirtualConsoleArgumentParser()
        parser.add_argument('path')

        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('-p', action='store_true', help="Show effective permissions for a given object")
        group.add_argument('-r', action=MergeListAction, nargs='+', help="Check if the user has some rights on a given object")
        return parser

    @db.ro_transact
    def execute(self, args):
        obj = self.traverse(args.path)
        if not obj:
            self.write("No such object %s\n" % args.path)
            return

        if args.p:
            self.write("Effective permissions: %s\n" % pretty_effective_perms(self.protocol.interaction, obj))
        elif args.r:
            self.check_rights(obj, args)

    def check_rights(self, obj, args):
        interaction = self.protocol.interaction
        obj = proxy_factory(obj, interaction)

        allowed = []
        denied = []
        for r in args.r:
            for i in r.split(','):
                i = i.strip()
                if i.startswith('@'):
                    i = i[1:]
                (allowed if interaction.checkPermission(i, obj) else denied).append(i)

        self.write("+%s:-%s\n" % (','.join('@' + i for i in allowed), ','.join('@' + i for i in denied)))


class GetAclCmd(Cmd):
    implements(ICmdArgumentsSyntax)

    command('getfacl')

    def arguments(self):
        parser = VirtualConsoleArgumentParser()
        parser.add_argument('paths', nargs='+')
        parser.add_argument('-v', action='store_true', help="show grants for every permission")
        return parser

    @db.ro_transact
    def execute(self, args):
        for path in args.paths:
            obj = self.traverse(path)
            if not obj:
                self.write("No such object %s\n" % path)
                continue

            self._do_print_acl(obj, args.v)

    def _do_print_acl(self, obj, verbose):
        prinrole = IPrincipalRoleManager(obj)
        auth = getUtility(IAuthentication, context=None)

        user_allow = collections.defaultdict(list)
        user_deny = collections.defaultdict(list)
        users = set()
        for role, principal, setting in prinrole.getPrincipalsAndRoles():
            users.add(principal)
            if setting.getName() == 'Allow':
                user_allow[principal].append(role)
            else:
                user_deny[principal].append(role)

        for principal in users:
            def formatted_perms(perms):
                prin = auth.getPrincipal(principal)
                typ = 'group' if isinstance(prin, Group) else 'user'
                if verbose:
                    def grants(i):
                        return ','.join('@%s' % i[0] for i in rolePermissionManager.getPermissionsForRole(i) if i[0] != 'oms.nothing')
                    return (typ, principal, ''.join('%s{%s}' % (Role.role_to_nick.get(i, '(%s)' % i), grants(i)) for i in sorted(perms)))
                else:
                    return (typ, principal, ''.join(Role.role_to_nick.get(i, '(%s)' % i) for i in sorted(perms)))

            if principal in user_allow:
                self.write("%s:%s:+%s\n" % formatted_perms(user_allow[principal]))
            if principal in user_deny:
                self.write("%s:%s:-%s\n" % formatted_perms(user_deny[principal]))


class SetAclCmd(Cmd):
    implements(ICmdArgumentsSyntax)

    command('setfacl')

    def arguments(self):
        parser = VirtualConsoleArgumentParser()
        parser.add_argument('paths', nargs='+')
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('-m', action='append', help="add an Allow ace: {u:[user]:permspec|g:[group]:permspec}")
        group.add_argument('-d', action='append', help="add an Deny ace: {u:[user]:permspec|g:[group]:permspec}")
        group.add_argument('-x', action='append', help="remove an ace: {u:[user]:permspec|g:[group]:permspec}")
        return parser

    @db.ro_transact
    def execute(self, args):
        try:
            for path in args.paths:
                obj = self.traverse(path)
                with self.protocol.interaction:
                    self._do_set_acl(obj, args.m, args.d, args.x)
        except NoSuchPermission as e:
            self.write("No such permission '%s'\n" % (e.message))
            transaction.abort()

    def _do_set_acl(self, obj, allow_perms, deny_perms, del_perms):
        prinrole = IPrincipalRoleManager(obj)
        auth = getUtility(IAuthentication, context=None)

        def mod_perm(what, setter, p):
            kind, principal, perms = p.split(':')
            if not perms:
                return

            prin = auth.getPrincipal(principal)
            if isinstance(prin, Group) and kind == 'u':
                self.write("No such user '%s', it's a group, perhaps you mean 'g:%s:%s'\n" % (principal, principal, perms))
                return
            elif type(prin) is User and kind == 'g':
                self.write("No such group '%s', it's an user (%s), perhaps you mean 'u:%s:%s'\n" % (principal, prin, principal, perms))
                return

            for perm in perms.strip():
                if perm not in Role.nick_to_role:
                    raise NoSuchPermission(perm)
                role = Role.nick_to_role[perm].id
                self.write("%s permission '%s', principal '%s'\n" % (what, role, principal))
                setter(role, principal)

        for p in allow_perms or []:
            mod_perm("Allowing", prinrole.assignRoleToPrincipal, p)

        for p in deny_perms or []:
            mod_perm("Denying", prinrole.removeRoleFromPrincipal, p)

        for p in del_perms or []:
            mod_perm("Unsetting", prinrole.unsetRoleForPrincipal, p)

        transaction.commit()
