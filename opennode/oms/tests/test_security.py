import os
import unittest

from nose.tools import eq_, assert_raises
from zope.annotation.interfaces import IAttributeAnnotatable
from zope.authentication.interfaces import IAuthentication
from zope.component import getUtility
from zope.interface import implementer
from zope.security.interfaces import Unauthorized
from zope.security.management import newInteraction, getInteraction, endInteraction, setSecurityPolicy
from zope.securitypolicy import interfaces
from zope.securitypolicy import zopepolicy
from zope.securitypolicy.principalpermission import principalPermissionManager as prinperG


from opennode.oms.model.model.base import IContainer
from opennode.oms.model.schema import model_to_dict
from opennode.oms.tests.test_compute import Compute
from opennode.oms.security import authentication
from opennode.oms.security.checker import proxy_factory
from opennode.oms.security.interaction import OmsSecurityPolicy
from opennode.oms.security import passwd
from opennode.oms.security.principals import User
from opennode.oms.tests.util import run_in_reactor


class SessionStub(object):

    def __init__(self, principal=None):
        self.principal = principal
        self.interaction = None


@implementer(IAttributeAnnotatable)
class DummyObject(object):
    pass


class Participation:
    interaction = None


class InteractionScope(object):
    def __init__(self, principal):
        self.principal = principal

    def __enter__(self):
        newInteraction(self.principal)
        return getInteraction()

    def __exit__(self, type, value, traceback):
        endInteraction()


class SecurityTestCase(unittest.TestCase):

    def _get_interaction(self, uid):
        auth = getUtility(IAuthentication, context=None)

        interaction = OmsSecurityPolicy()
        sess = SessionStub(auth.getPrincipal(uid))
        interaction.add(sess)
        return interaction

    def make_compute(self, hostname=u'tux-for-test', state=u'active', memory=2000):
        res = Compute(hostname, state, memory)
        res.architecture = 'linux'
        return res

    @run_in_reactor
    def test_test(self):
        auth = getUtility(IAuthentication, context=None)
        auth.registerPrincipal(User('user1'))
        auth.registerPrincipal(User('user2'))

        # setup some fake permissions to the test principals
        prinperG.grantPermissionToPrincipal('read', 'user1')
        prinperG.grantPermissionToPrincipal('zope.Nothing', 'user2')

        # set up interactions
        interaction_user1 = self._get_interaction('user1')
        interaction_user2 = self._get_interaction('user2')

        # get the object being secured
        compute = self.make_compute()
        eq_(compute.architecture, 'linux')

        # get the proxies for the corresponding interactions
        compute_proxy_user1 = proxy_factory(compute, interaction_user1)
        compute_proxy_user2 = proxy_factory(compute, interaction_user2)

        # check an authorized access
        eq_(compute_proxy_user1.architecture, 'linux')

        # check an unauthorized access
        with assert_raises(Unauthorized):
            eq_(compute_proxy_user2.architecture, 'linux')

        # check a default unauthorized access
        with assert_raises(Unauthorized):
            eq_(compute_proxy_user1.state, 'active')

    @run_in_reactor
    def test_adapt(self):
        auth = getUtility(IAuthentication, context=None)
        auth.registerPrincipal(User('user1'))
        interaction = self._get_interaction('user1')

        # get the object being secured
        compute = self.make_compute()
        compute_proxy = proxy_factory(compute, interaction)

        eq_(IContainer(compute), IContainer(compute_proxy))

    @run_in_reactor
    def test_schema(self):
        auth = getUtility(IAuthentication, context=None)
        auth.registerPrincipal(User('userSchema'))
        prinperG.grantPermissionToPrincipal('read', 'userSchema')
        prinperG.grantPermissionToPrincipal('modify', 'userSchema')

        interaction = self._get_interaction('userSchema')

        # get the object being secured
        compute = self.make_compute()
        compute_proxy = proxy_factory(compute, interaction)

        eq_(model_to_dict(compute), model_to_dict(compute_proxy))
        #print model_to_dict(compute)
        #print model_to_dict(compute_proxy)

    def test_ownership_concept(self):
        alice = User('alice')
        bob = User('bob')

        oldpolicy = setSecurityPolicy(zopepolicy.ZopeSecurityPolicy)

        def create_object():
            obj = DummyObject()
            roleper = interfaces.IRolePermissionManager(obj)
            roleper.grantPermissionToRole('anything', 'owner')
            return obj

        def set_owner(obj, principal):
            prinrole = interfaces.IPrincipalRoleManager(obj)
            prinrole.assignRoleToPrincipal('owner', principal.id)

        aobj = create_object()
        bobj = create_object()

        bob_p = Participation()
        bob_p.principal = bob

        alice_p = Participation()
        alice_p.principal = alice

        set_owner(aobj, alice)
        set_owner(bobj, bob)

        with InteractionScope(alice_p) as alice_int:
            # alice is owner of aobj, but cannot access bobj
            assert not alice_int.checkPermission('anything', bobj)
            assert alice_int.checkPermission('anything', aobj)

        with InteractionScope(bob_p) as bob_int:
            # bob is owner of bobj, but cannot access aobj
            assert bob_int.checkPermission('anything', bobj)
            assert not bob_int.checkPermission('anything', aobj)

        setSecurityPolicy(oldpolicy)

    def test_with(self):
        interaction = self._get_interaction('user1')

        def dummy():
            yield 2
            with interaction:
                yield 1

        with assert_raises(Exception):
            list(dummy())


class MockConfig(object):

    _settings = {'auth': {
        'passwd_file': '/tmp/oms_passwd',
        'restricted_users': ''
    }}

    def get_base_dir(self):
        return '/tmp'

    def get(self, key, inkey):
        return self._settings[key][inkey]

    def getstring(self, key, inkey, default=object()):
        try:
            return self._settings[key][inkey]
        except KeyError:
            if default is not self.NO_DEFAULT:
                return default
            raise


config = MockConfig()


def mock_get_config():
    global config
    return config


class TestPasswd(unittest.TestCase):

    username = 'test_update_passwd'

    def setUp(self):
        passwd.get_config = mock_get_config
        passwd_file = passwd.get_config().get('auth', 'passwd_file')

        if not os.path.exists(passwd_file):
            with open(passwd_file, 'a'):
                pass

        self.orig_get_config = passwd.get_config

        passwd.delete_user(self.username)

        self.assertRaises(self.failureException,
                          self.assertUserExists, self.username)

    def tearDown(self):
        passwd_file = passwd.get_config().get('auth', 'passwd_file')
        os.unlink(passwd_file)
        passwd.get_config = self.orig_get_config

    def find_user_line(self, username):
        passwd_file = passwd.get_config().get('auth', 'passwd_file')
        with open(passwd_file) as f:
            lines = f.readlines()

        for line in lines:
            if line.startswith(username + ':'):
                return line

    def assertUserExists(self, username):
        self.assertTrue(self.find_user_line(username))

    def assertUserPassword(self, username, password):
        line = self.find_user_line(username)
        _, pwu, group = line.split(':', 2)
        pw = authentication.ssha_hash(username, password, pwu)
        self.assertEquals(pw, pwu)

    def assertUserGroup(self, username, group):
        line = self.find_user_line(username)
        self.assertTrue(line)
        _, _, groupu = line.split(':', 2)
        if ':' in groupu:
            groupu, _ = groupu.split(':', 1)

        self.assertEquals(groupu, group)

    def test_password_hash_consistency_with_random_salt(self):
        hash_passwd = passwd.hash_pw('password', saltf=passwd.get_salt)
        hash_auth = authentication.ssha_hash('', 'password', hash_passwd)
        self.assertEquals(hash_passwd, hash_auth)

    def test_password_hash_consistency_with_no_salt(self):
        hash_passwd = passwd.hash_pw('password', saltf=passwd.get_salt_dummy)
        hash_auth = authentication.ssha_hash('', 'password', hash_passwd)
        self.assertEquals(hash_passwd, hash_auth)

    def test_add_delete_user(self):
        passwd_file = passwd.get_config().get('auth', 'passwd_file')
        if not os.path.exists(passwd_file):
            with open(passwd_file, 'w') as f:
                f.write('')

        try:
            passwd.add_user(self.username, 'password')
            self.assertUserExists(self.username)
            self.assertUserPassword(self.username, 'password')
        finally:
            passwd.delete_user(self.username)

        passwd.delete_user(self.username)
        self.assertRaises(self.failureException,
                          self.assertUserExists, self.username)

    def test_update_passwd_mock(self):
        passwd_file = passwd.get_config().get('auth', 'passwd_file')
        if not os.path.exists(passwd_file):
            with open(passwd_file, 'w') as f:
                f.write('')

        passwd.add_user(self.username, 'password')
        self.assertUserExists(self.username)
        self.assertUserPassword(self.username, 'password')

        try:
            class DummySha1(object):
                def __init__(self, string):
                    self.string = string

                def update(self, string):
                    pass

                def digest(self):
                    return self.string

            sha1 = passwd.hashlib.sha1
            encode = passwd.encode
            ssha_hash = authentication.ssha_hash
            get_salt = passwd.get_salt

            try:
                passwd.get_salt = passwd.get_salt_dummy
                passwd.hashlib.sha1 = DummySha1
                passwd.encode = lambda x: x
                passwd.decode = lambda x: x
                authentication.ssha_hash = lambda u, x, y: '{SSHA}' + x + y[-4:]

                passwd.update_passwd(self.username, password='newpassword',
                                     force_askpass=False, group='somegroup')

                self.assertUserExists(self.username)
                self.assertUserPassword(self.username, 'newpassword')
            finally:
                passwd.hashlib.sha1 = sha1
                passwd.encode = encode
                authentication.ssha_hash = ssha_hash
                passwd.get_salt = get_salt

            passwd.update_passwd(self.username, password='newpassword',
                                 force_askpass=False, group='somegroup')
            self.assertUserExists(self.username)
            self.assertUserPassword(self.username, 'newpassword')

        finally:
            passwd.delete_user(self.username)

    def test_update_passwd(self):
        passwd.add_user(self.username + 'asdfghj', 'asdfghjkl')
        passwd.add_user(self.username, 'password')
        passwd.add_user(self.username + 'poiuytr', 'poiuytr')
        self.assertUserExists(self.username)
        self.assertUserPassword(self.username, 'password')

        try:
            passwd.update_passwd(self.username, password='pdadsdasdas',
                                 force_askpass=False, group='somegroup')
            self.assertUserExists(self.username)
            self.assertUserPassword(self.username, 'pdadsdasdas')
            self.assertRaises(self.failureException, self.assertUserPassword, self.username, 'p')
        finally:
            passwd.delete_user(self.username)

    def test_update_password_changes_single_record(self):
        passwd.add_user(self.username, 'password')
        passwd.add_user(self.username + '1', 'password')
        passwd.add_user(self.username + '2', 'password')
        self.assertUserExists(self.username)
        self.assertUserExists(self.username + '1')
        self.assertUserExists(self.username + '2')

        self.assertUserPassword(self.username, 'password')
        self.assertUserPassword(self.username + '1', 'password')
        self.assertUserPassword(self.username + '2', 'password')

        try:
            passwd.update_passwd(self.username, password='newpassword',
                                 force_askpass=False, group='somegroup')
            self.assertUserExists(self.username)
            self.assertUserPassword(self.username, 'newpassword')
            self.assertUserPassword(self.username + '1', 'password')
            self.assertUserPassword(self.username + '2', 'password')
        finally:
            passwd.delete_user(self.username)
            passwd.delete_user(self.username + '1')
            passwd.delete_user(self.username + '2')
