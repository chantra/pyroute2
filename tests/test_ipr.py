import uuid
import socket
from pyroute2 import IPRoute
from pyroute2.netlink import NetlinkError
from multiprocessing import Event
from multiprocessing import Process
from utils import grep
from utils import require_user
from utils import get_ip_addr
from utils import get_ip_link
from utils import get_ip_route
from utils import get_ip_rules
from utils import create_link
from utils import remove_link


def _run_remote_client(url, func, key=None, cert=None, ca=None):
    ip = IPRoute(host=url, key=key, cert=cert, ca=ca)
    getattr(ip, func)()
    ip.release()


def _run_remote_uplink(url, connect, release,
                       key=None, cert=None, ca=None):
    ip = IPRoute()
    ip.serve(url, key=key, cert=cert, ca=ca)
    ip.iothread.secret = 'bala'
    connect.set()
    release.wait()
    ip.release()


def _assert_servers(ip, num):
    pass


def _assert_clients(ip, num):
    pass


def _assert_uplinks(ip, num):
    assert len(ip.uids) == num


class TestSetup(object):

    def test_simple(self):
        ip = IPRoute()
        _assert_uplinks(ip, 1)
        ip.monitor()
        ip.monitor(False)
        ip.release()
        _assert_uplinks(ip, 0)

    def _test_noautoconnect(self):
        ip = IPRoute(do_connect=False)
        _assert_uplinks(ip, 0)
        addr = ip.connect()
        _assert_uplinks(ip, 1)
        ip.disconnect(addr)
        _assert_uplinks(ip, 0)
        ip.release()

    def test_serve(self, url=None, key=None, cert=None, ca=None):
        url = url or 'unix://\0nose_tests_socket'
        ip = IPRoute()
        ip.serve(url, key=key, cert=cert, ca=ca)
        ip.shutdown(url)
        ip.release()

    def test_serve_tls(self):
        url = 'tls://127.0.0.1:9876'
        key = 'server.key'
        cert = 'server.crt'
        ca = 'ca.crt'
        return self.test_serve(url, key, cert, ca)

    def test_serve_unix_tls(self):
        url = 'unix+tls://\0nose_tests_socket'
        key = 'server.key'
        cert = 'server.crt'
        ca = 'ca.crt'
        return self.test_serve(url, key, cert, ca)

    def test_serve_ssl(self):
        url = 'ssl://127.0.0.1:9876'
        key = 'server.key'
        cert = 'server.crt'
        ca = 'ca.crt'
        return self.test_serve(url, key, cert, ca)

    def test_serve_unix_ssl(self):
        url = 'unix+ssl://\0nose_tests_socket'
        key = 'server.key'
        cert = 'server.crt'
        ca = 'ca.crt'
        return self.test_serve(url, key, cert, ca)


class TestServerSide(object):

    def testServer(self):
        url = 'unix://\0%s' % (uuid.uuid4())
        ip = IPRoute()
        ip.serve(url)
        target = Process(target=_run_remote_client,
                         args=(url, 'get_links'))
        target.start()
        target.join()
        ip.release()


class TestSetupUplinks(object):

    def _test_remote(self, url):
        connect = Event()
        release = Event()
        target = Process(target=_run_remote_uplink,
                         args=(url, connect, release))
        target.daemon = True
        target.start()
        connect.wait()
        ip = IPRoute(host=url)
        ip.release()
        release.set()

    def test_unix_abstract_remote(self):
        self._test_remote('unix://\0nose_tests_socket')

    def test_tcp_remote(self):
        self._test_remote('tcp://127.0.0.1:9821')


class TestMisc(object):

    def setup(self):
        self.ip = IPRoute()

    def teardown(self):
        self.ip.release()

    def test_addrpool_expand(self):
        # see coverage
        for i in range(100):
            self.ip.get_addr()


def _callback(envelope, msg, obj):
    obj.cb_counter += 1


class TestData(object):

    def setup(self):
        create_link('dummyX', 'dummy')
        self.release = Event()
        self.ip = IPRoute()
        self.dev = self.ip.link_lookup(ifname='dummyX')

    def teardown(self):
        self.ip.release()
        self.release.set()
        remove_link('dummyX')
        remove_link('bala')

    def test_add_addr(self):
        require_user('root')
        dev = self.dev[0]
        self.ip.addr('add', dev, address='172.16.0.1', mask=24)
        assert '172.16.0.1/24' in get_ip_addr()

    def test_remove_link(self):
        require_user('root')
        create_link('bala', 'dummy')
        dev = self.ip.link_lookup(ifname='bala')[0]
        try:
            self.ip.link_remove(dev)
        except NetlinkError:
            pass
        assert len(self.ip.link_lookup(ifname='bala')) == 0

    def test_route(self):
        require_user('root')
        create_link('bala', 'dummy')
        dev = self.ip.link_lookup(ifname='bala')[0]
        self.ip.link('set', index=dev, state='up')
        self.ip.addr('add', dev, address='172.16.0.2', mask=24)
        self.ip.route('add',
                      prefix='172.16.1.0',
                      mask=24,
                      gateway='172.16.0.1')
        assert grep('ip route show', pattern='172.16.1.0/24.*172.16.0.1')
        remove_link('bala')

    def test_updown_link(self):
        require_user('root')
        dev = self.dev[0]
        assert not (self.ip.get_links(dev)[0]['flags'] & 1)
        try:
            self.ip.link_up(dev)
        except NetlinkError:
            pass
        assert self.ip.get_links(dev)[0]['flags'] & 1
        try:
            self.ip.link_down(dev)
        except NetlinkError:
            pass
        assert not (self.ip.get_links(dev)[0]['flags'] & 1)

    def test_callbacks_positive(self):
        require_user('root')
        dev = self.dev[0]

        self.cb_counter = 0
        self.ip.register_callback(_callback,
                                  lambda e, x: x.get('index', None) == dev,
                                  (self, ))
        self.test_updown_link()
        assert self.cb_counter > 0
        self.ip.unregister_callback(_callback)

    def test_callbacks_negative(self):
        require_user('root')

        self.cb_counter = 0
        self.ip.register_callback(_callback,
                                  lambda e, x: x.get('index', None) == 'bala',
                                  (self, ))
        self.test_updown_link()
        assert self.cb_counter == 0
        self.ip.unregister_callback(_callback)

    def test_rename_link(self):
        require_user('root')
        dev = self.dev[0]
        try:
            self.ip.link_rename(dev, 'bala')
        except NetlinkError:
            pass
        assert len(self.ip.link_lookup(ifname='bala')) == 1
        try:
            self.ip.link_rename(dev, 'dummyX')
        except NetlinkError:
            pass
        assert len(self.ip.link_lookup(ifname='dummyX')) == 1

    def test_rules(self):
        assert len(get_ip_rules('-4')) == \
            len(self.ip.get_rules(socket.AF_INET))
        assert len(get_ip_rules('-6')) == \
            len(self.ip.get_rules(socket.AF_INET6))

    def test_addr(self):
        assert len(get_ip_addr()) == len(self.ip.get_addr())

    def test_links(self):
        assert len(get_ip_link()) == len(self.ip.get_links())

    def test_one_link(self):
        lo = self.ip.get_links(1)[0]
        assert lo.get_attr('IFLA_IFNAME') == 'lo'

    def test_routes(self):
        assert len(get_ip_route()) == \
            len(self.ip.get_routes(family=socket.AF_INET, table=255))


class TestProxyData(TestData):

    def setup(self):
        create_link('dummyX', 'dummy')
        t_url = 'unix://\0%s' % (uuid.uuid4())
        p_url = 'unix://\0%s' % (uuid.uuid4())
        self.connect = Event()
        self.release = Event()

        target = Process(target=_run_remote_uplink,
                         args=(t_url, self.connect, self.release))
        target.daemon = True
        target.start()
        self.connect.wait()
        self.connect.clear()

        proxy = Process(target=_run_remote_uplink,
                        args=(p_url, self.connect, self.release))
        proxy.daemon = True
        proxy.start()
        self.connect.wait()
        self.connect.clear()

        self.ip = IPRoute(do_connect=False)
        link, proxy = self.ip.connect(p_url)
        self.ip.register('bala', proxy)
        link, host = self.ip.connect(t_url, addr=proxy)
        service = self.ip.discover(self.ip.default_target, addr=host)

        self.ip.default_peer = host
        self.ip.default_dport = service

        self.dev = self.ip.link_lookup(ifname='dummyX')


class TestRemoteData(TestData):

    def setup(self):
        create_link('dummyX', 'dummy')
        url = 'unix://\0%s' % (uuid.uuid4())
        self.connect = Event()
        self.release = Event()
        target = Process(target=_run_remote_uplink,
                         args=(url, self.connect, self.release))
        target.daemon = True
        target.start()
        self.connect.wait()
        self.ip = IPRoute(host=url)
        self.dev = self.ip.link_lookup(ifname='dummyX')


class TestSSLData(TestData):
    ssl_proto = 'ssl'

    def setup(self):
        create_link('dummyX', 'dummy')
        url = 'unix+%s://\0%s' % (self.ssl_proto, uuid.uuid4())
        self.connect = Event()
        self.release = Event()
        target = Process(target=_run_remote_uplink,
                         args=(url,
                               self.connect,
                               self.release,
                               'server.key',
                               'server.crt',
                               'ca.crt'))
        target.daemon = True
        target.start()
        self.connect.wait()
        self.ip = IPRoute(host=url,
                          key='client.key',
                          cert='client.crt',
                          ca='ca.crt')
        self.dev = self.ip.link_lookup(ifname='dummyX')


class TestTLSData(TestSSLData):
    ssl_proto = 'tls'
