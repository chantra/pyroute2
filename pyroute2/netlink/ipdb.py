'''
Experimental IP database module. Please, do not use it
unless you understand what are you doing.

Quick start:
    from pyroute2 import IPDB
    ip = IPDB()
    ip['eth0']['address'] = '00:11:22:33:44:55'
    ip['eth0']['ifname'] = 'bala'
    ip['eth0']['txqlen'] = 2000
    ip['eth0'].commit()
'''
import uuid
import platform
import threading
try:
    from Queue import Empty
except ImportError:
    from queue import Empty

from socket import AF_INET
from socket import AF_INET6
from pyroute2.common import Dotkeys
from pyroute2.netlink import NetlinkError
from pyroute2.netlink.iproute import IPRoute
from pyroute2.netlink.rtnl.ifinfmsg import ifinfmsg
from pyroute2.netlink.rtnl.tcmsg import tcmsg

tc_fields = [tcmsg.nla2name(i[0]) for i in tcmsg.nla_map]
nla_fields = [ifinfmsg.nla2name(i[0]) for i in ifinfmsg.nla_map]
nla_fields.append('flags')
nla_fields.append('mask')
nla_fields.append('change')
nla_fields.append('state')
nla_fields.append('removal')


_ANCIENT_PLATFORM = platform.dist()[:2] == ('redhat', '6.4')
# How long should we wait on EACH commit() checkpoint: for ipaddr,
# ports etc. That's not total commit() timeout.
_SYNC_TIMEOUT = 3

_FAIL_COMMIT = 0b00000001
_FAIL_ROLLBACK = 0b00000010
_FAIL_MASK = 0b11111111


def clear_fail_bit(bit):
    global _FAIL_MASK
    _FAIL_MASK &= ~(_FAIL_MASK & bit)


def set_fail_bit(bit):
    global _FAIL_MASK
    _FAIL_MASK |= bit


def set_ancient(flag):
    global _ANCIENT_PLATFORM
    _ANCIENT_PLATFORM = flag


def get_addr_nla(msg):
    '''
    Incosistency in Linux IP addressing scheme is that
    IPv4 uses IFA_LOCAL to store interface's ip address,
    and IPv6 uses for the same IFA_ADDRESS.

    IPv4 sets IFA_ADDRESS to == IFA_LOCAL or to a
    tunneling endpoint.
    '''
    nla = None
    if msg['family'] == AF_INET:
        nla = msg.get_attr('IFA_LOCAL')
    elif msg['family'] == AF_INET6:
        nla = msg.get_attr('IFA_ADDRESS')
    return nla


class LinkedSet(set):

    def __init__(self, *argv, **kwarg):
        set.__init__(self, *argv, **kwarg)
        self.lock = threading.RLock()
        self.target = threading.Event()
        self._ct = None
        self.raw = {}
        self.links = []

    def set_target(self, value):
        with self.lock:
            if value is None:
                self._ct = None
                self.target.clear()
            else:
                self._ct = set(value)
                self.target.clear()

    def check_target(self):
        with self.lock:
            if self._ct is not None:
                if self == self._ct:
                    self._ct = None
                    self.target.set()

    def add(self, key, raw=None):
        with self.lock:
            if key not in self:
                self.raw[key] = raw
                set.add(self, key)
                for link in self.links:
                    link.add(key, raw)
            self.check_target()

    def remove(self, key, raw=None):
        with self.lock:
            set.remove(self, key)
            for link in self.links:
                if key in link:
                    link.remove(key)
            self.check_target()

    def connect(self, link):
        assert isinstance(link, LinkedSet)
        self.links.append(link)

    def __repr__(self):
        return repr(list(self))


class State(object):

    def __init__(self, lock=None):
        self.lock = lock or threading.Lock()
        self.flag = 0

    def acquire(self):
        self.lock.acquire()
        self.flag += 1

    def release(self):
        assert self.flag > 0
        self.flag -= 1
        self.lock.release()

    def is_set(self):
        return self.flag

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


def update(f):
    def decorated(self, *argv, **kwarg):
        # obtain update lock
        tid = None
        direct = True
        with self._write_lock:
            dcall = kwarg.pop('direct', False)
            if dcall:
                self._direct_state.acquire()

            direct = self._direct_state.is_set()
            if not direct:
                # 1. begin transaction for 'direct' type
                if self._mode == 'direct':
                    tid = self.begin()
                # 2. begin transaction, if there is none
                elif self._mode == 'implicit':
                    if not self._tids:
                        self.begin()
                # 3. require open transaction for 'explicit' type
                elif self._mode == 'explicit':
                    if not self._tids:
                        raise TypeError('start a transaction first')
                # 4. transactions can not require transactions :)
                elif self._mode == 'snapshot':
                    direct = True
                # do not support other modes
                else:
                    raise TypeError('transaction mode not supported')
                # now that the transaction _is_ open
            f(self, direct, *argv, **kwarg)

            if dcall:
                self._direct_state.release()

        if tid:
            # close the transaction for 'direct' type
            self.commit(tid)
    return decorated


class IPLinkRequest(dict):

    def __init__(self, interface=None):
        dict.__init__(self)
        if interface:
            self.update(interface)

    def update(self, interface):
        for key in interface:
            if interface[key] is not None:
                self[key] = interface[key]

    def __setitem__(self, key, value):
        if key == 'kind':
            if 'IFLA_LINKINFO' not in self:
                self['IFLA_LINKINFO'] = {'attrs': []}
            nla = ['IFLA_INFO_KIND', value]
            # FIXME: we need to replace, not add
            self['IFLA_LINKINFO']['attrs'].append(nla)
        elif key == 'vlan_id':
            if 'IFLA_LINKINFO' not in self:
                self['IFLA_LINKINFO'] = {'attrs': []}
            nla = ['IFLA_INFO_DATA', {'attrs': [['IFLA_VLAN_ID', value]]}]
            # FIXME: we need to replace, not add
            self['IFLA_LINKINFO']['attrs'].append(nla)
        else:
            dict.__setitem__(self, key, value)


class Transactional(Dotkeys):
    '''
    An utility class that implements common transactional logic.
    '''
    def __init__(self, nl=None, ipdb=None, mode='direct'):
        self.nl = nl
        self.uid = uuid.uuid4()
        self.ipdb = ipdb
        self.last_error = None
        self._callbacks = []
        self._fields = []
        self._tids = []
        self._transactions = {}
        self._mode = mode
        self._write_lock = threading.RLock()
        self._direct_state = State(self._write_lock)
        self._linked_sets = set()

    def register_callback(self, callback):
        self._callbacks.append(callback)

    def unregister_callback(self, callback):
        for cb in self._callbacks:
            if callback == cb:
                self._callbacks.pop(self._callbacks.index(cb))

    def pick(self, detached=True):
        '''
        Get a snapshot of the object. Can be of two
        types:
            * detached=True -- (default) "true" snapshot
            * detached=False -- keep ip addr set updated from OS

        Please note, that "updated" doesn't mean "in sync".
        The reason behind this logic is that snapshots can be
        used as transactions.
        '''
        with self._write_lock:
            res = self.__class__(nl=self.nl, mode='snapshot')
            for key in tuple(self.keys()):
                if key in nla_fields:
                    res[key] = self[key]
            for key in self._linked_sets:
                res[key] = LinkedSet(self[key])
                if self.ipdb is not None and not detached:
                    self[key].connect(res[key])
            return res

    def __enter__(self):
        # FIXME: use a bitmask?
        if self._mode not in ('implicit', 'explicit'):
            raise TypeError('context managers require a transactional mode')
        if not self._tids:
            self.begin()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # apply transaction only if there was no error
        if exc_type is None:
            try:
                self.commit()
            except Exception as e:
                self.last_error = e
                raise e

    def __repr__(self):
        res = {}
        for i in self:
            if self[i] is not None:
                res[i] = self[i]
        return res.__repr__()

    def __sub__(self, vs):
        '''
        '''
        res = self.__class__(nl=self.nl, mode='snapshot')
        with self._direct_state:
            # simple keys
            for key in self:
                if (key in self._fields) and \
                        ((key not in vs) or (self[key] != vs[key])):
                    res[key] = self[key]
        for key in self._linked_sets:
            diff = LinkedSet(self[key] - vs[key])
            if diff:
                res[key] = diff
        return res

    def commit(self, *args, **kwarg):
        pass

    def begin(self):
        '''
        Start new transaction
        '''
        # keep snapshot's ip addr set updated from the OS
        # it is required by the commit logic
        t = self.pick(detached=False)
        self._transactions[t.uid] = t
        self._tids.append(t.uid)
        return t.uid

    def last(self):
        '''
        Return last open transaction
        '''
        if not self._tids:
            raise TypeError('start a transaction first')

        return self._transactions[self._tids[-1]]

    def review(self):
        '''
        Review last open transaction
        '''
        if not self._tids:
            raise TypeError('start a transaction first')

        added = self.last() - self
        removed = self - self.last()
        for key in self._linked_sets:
            added['-{}'.format(key)] = removed[key]
            added['+{}'.format(key)] = added[key]
            del added[key]
        return added

    def drop(self):
        '''
        Drop all not applied changes and rollback transaction.
        '''
        del self._transactions[self._tids.pop()]

    @update
    def __setitem__(self, direct, key, value):
        if not direct:
            transaction = self.last()
            transaction[key] = value
        else:
            Dotkeys.__setitem__(self, key, value)

    @update
    def __delitem__(self, direct, key):
        if not direct:
            transaction = self.last()
            if key in transaction:
                del transaction[key]
        else:
            Dotkeys.__delitem__(self, key)

    def set_item(self, key, value):
        with self._direct_state:
            self[key] = value

    def del_item(self, key):
        with self._direct_state:
            del self[key]


class TrafficControl(Transactional):
    '''
    '''
    def __init__(self, nl=None, ipdb=None, mode='direct'):
        Transactional.__init__(self, nl, ipdb, mode)
        self._fields = tc_fields

    def load(self, msg):
        with self._direct_state:
            self.update(msg)
            self['kind'] = msg.get_attr('TCA_KIND')

    def set_filter(self, kind, **kwarg):
        pass

    def set_control(self, kind, **kwarg):
        pass

    def remove(self):
        pass


class Interface(Transactional):
    '''
    Objects of this class represent network interface and
    all related objects:
        * addresses
        * (todo) neighbors
        * (todo) routes

    Interfaces provide transactional model and can act as
    context managers. Any attribute change implicitly
    starts a transaction. The transaction can be managed
    with three methods:
        * review() -- review changes
        * rollback() -- drop all the changes
        * commit() -- try to apply changes

    If anything will go wrong during transaction commit,
    it will be rolled back authomatically and an
    exception will be raised. Failed transaction review
    will be attached to the exception.
    '''
    def __init__(self, nl=None, ipdb=None, mode='direct'):
        '''
        One can use interface objects standalone as
        well as in connection with ipdb object. Standalone
        usage, though, is discouraged.

        Parameters:
            * nl   -- IPRoute() reference
            * ipdb -- ipdb() reference
            * mode -- transaction mode
        '''
        Transactional.__init__(self, nl, ipdb, mode)
        self.cleanup = ('header',
                        'linkinfo',
                        'af_spec',
                        'attrs',
                        'event',
                        'map',
                        'stats',
                        'stats64')
        self.ingress = None
        self.egress = None
        self._exists = False
        self._fields = nla_fields
        self._load_event = threading.Event()
        self._linked_sets.add('ipaddr')
        self._linked_sets.add('ports')
        # 8<-----------------------------------
        # local setup: direct state is required
        with self._direct_state:
            self['ipaddr'] = LinkedSet()
            self['ports'] = LinkedSet()
            for i in nla_fields:
                self[i] = None
            for i in ('state', 'change', 'mask'):
                del self[i]
        # 8<-----------------------------------

    def __hash__(self):
        return self['index']

    @property
    def if_master(self):
        '''
        [property] Link to the parent interface -- if it exists
        '''
        ret = [self[i] for i in ('link', 'master')
               if (i in self) and isinstance(self[i], int)] or [None]
        return ret[0]

    def load(self, dev):
        '''
        Update the interface info from RTM_NEWLINK message.

        This call always bypasses open transactions, loading
        changes directly into the interface data.
        '''
        with self._direct_state:
            self._exists = True
            self.update(dev)
            self._attrs = set()
            for (name, value) in dev['attrs']:
                norm = ifinfmsg.nla2name(name)
                self._attrs.add(norm)
                self[norm] = value
            # load interface kind
            linkinfo = dev.get_attr('IFLA_LINKINFO')
            if linkinfo is not None:
                kind = linkinfo.get_attr('IFLA_INFO_KIND')
                if kind is not None:
                    self['kind'] = kind
                    if kind == 'vlan':
                        data = linkinfo.get_attr('IFLA_INFO_DATA')
                        self['vlan_id'] = data.get_attr('IFLA_VLAN_ID')
            # the rest is possible only when interface
            # is used in IPDB, not standalone
            if self.ipdb is not None:
                # connect IP address set from IPDB
                self['ipaddr'] = self.ipdb.ipaddr[self['index']]
            # finally, cleanup all not needed
            for item in self.cleanup:
                if item in self:
                    del self[item]

            self.sync()

    def sync(self):
        self._load_event.set()

    @update
    def add_ip(self, direct, ip, mask=None):
        # split mask
        if mask is None:
            ip, mask = ip.split('/')
            mask = int(mask, 0)
        # FIXME: make it more generic
        # skip IPv6 link-local addresses
        if ip[:4] == 'fe80' and mask == 64:
            return
        if not direct:
            transaction = self.last()
            transaction.add_ip(ip, mask)
        else:
            self['ipaddr'].add((ip, mask))

    @update
    def del_ip(self, direct, ip, mask=None):
        if mask is None:
            ip, mask = ip.split('/')
            mask = int(mask, 0)
        if not direct:
            transaction = self.last()
            if (ip, mask) in transaction['ipaddr']:
                transaction.del_ip(ip, mask)
        else:
            self['ipaddr'].remove((ip, mask))

    @update
    def add_port(self, direct, port):
        if isinstance(port, Interface):
            port = port['index']
        if not direct:
            transaction = self.last()
            transaction.add_port(port)
        else:
            self['ports'].add(port)

    @update
    def del_port(self, direct, port):
        if isinstance(port, Interface):
            port = port['index']
        if not direct:
            transaction = self.last()
            if port in transaction['ports']:
                transaction.del_port(port)
        else:
            # FIXME: do something with it, please
            if self.ipdb and _ANCIENT_PLATFORM and \
                    'master' in self.ipdb[port]:
                self.ipdb[port].del_item('master')
            self['ports'].remove(port)

    def reload(self):
        '''
        Reload interface information
        '''
        self._load_event.clear()
        try:
            self.nl.get_links(self['index'])
        except Empty:
            raise IOError('lost netlink connection')
        self._load_event.wait()

    def commit(self, tid=None, transaction=None, rollback=False):
        '''
        Commit transaction. In the case of exception all
        changes applied during commit will be reverted.
        '''
        error = None
        added = None
        removed = None
        if tid:
            transaction = self._transactions[tid]
        else:
            transaction = transaction or self.last()

        # if the interface does not exist, create it first ;)
        if not self._exists:
            request = IPLinkRequest(self)
            self.ipdb._links_event.clear()
            try:
                self.nl.link('add', **request)
            except Exception as e:
                # on failure, invalidate the interface and detach it
                # from the parent
                # 1. drop the IPRoute() link
                self.nl = None
                # 2. clean up ipdb
                self.ipdb.detach(self['index'])
                self.ipdb.detach(self['ifname'])
                # 3. invalidate the interface
                with self._direct_state:
                    for i in tuple(self.keys()):
                        del self[i]
                # 4. the rest
                self._mode = 'invalid'
                # raise the exception
                raise e

            # all is OK till now, so continue
            # we do not know what to load, so load everything
            self.nl.get_links()
            self.ipdb._links_event.wait()

        # now we have our index and IP set and all other stuff
        snapshot = self.pick()

        try:
            removed = snapshot - transaction
            added = transaction - snapshot

            # 8<---------------------------------------------
            # IP address changes
            self['ipaddr'].set_target(transaction['ipaddr'])

            for i in removed['ipaddr']:
                # When you remove a primary IP addr, all subnetwork
                # can be removed. In this case you will fail, but
                # it is OK, no need to roll back
                try:
                    self.nl.addr('delete', self['index'], i[0], i[1])
                except NetlinkError as x:
                    # bypass only errno 99, 'Cannot assign address'
                    if x.code != 99:
                        raise x

            for i in added['ipaddr']:
                self.nl.addr('add', self['index'], i[0], i[1])

            if removed['ipaddr'] or added['ipaddr']:
                self['ipaddr'].target.wait(_SYNC_TIMEOUT)
                assert self['ipaddr'].target.is_set()

            # 8<---------------------------------------------
            # Interface slaves
            self['ports'].set_target(transaction['ports'])

            for i in removed['ports']:
                # detach the port
                self.nl.link('set', index=i, master=0)

            for i in added['ports']:
                # enslave the port
                self.nl.link('set', index=i, master=self['index'])

            if removed['ports'] or added['ports']:
                self.nl.get_links(*(removed['ports'] | added['ports']))
                self['ports'].target.wait(_SYNC_TIMEOUT)
                assert self['ports'].target.is_set()

            # 8<---------------------------------------------
            # Interface changes
            request = IPLinkRequest()
            for key in added:
                if key in nla_fields:
                    request[key] = added[key]

            # apply changes only if there is something to apply
            if any([request[x] is not None for x in request]):
                self.nl.link('set', index=self['index'], **request)

            # 8<---------------------------------------------
            # Interface removal
            if added.get('removal'):
                self._load_event.clear()
                self.nl.link('delete', index=self['index'])
                self._load_event.wait(_SYNC_TIMEOUT)
                assert self._load_event.is_set()
                self.drop()
                self._mode = 'invalid'
                return
            # 8<---------------------------------------------

            if rollback:
                assert _FAIL_ROLLBACK & _FAIL_MASK
            else:

                # 8<-----------------------------------------
                # Iterate callback chain
                for cb in self._callbacks:
                    # An exception will rollback the transaction
                    cb(snapshot, transaction)
                # 8<-----------------------------------------

                assert _FAIL_COMMIT & _FAIL_MASK

        except Exception as e:
            # something went wrong: roll the transaction back
            if not rollback:
                self.reload()
                # 8<-----------------------------------------
                # That's a hack, but we have to use it, since
                # OS response can be not so fast
                # * inject added IPs directly into self
                # * wipe removed IPs from the interface data
                #
                # This info will be used to properly roll back
                # changes.
                #
                # Still, there is a possibility that the
                # rollback will run even before IP addrs will
                # be assigned. But we can not cope with that.
                with self._direct_state:
                    if removed:
                        for i in removed['ipaddr']:
                            try:
                                self['ipaddr'].remove(i)
                            except KeyError:
                                pass
                    if added:
                        for i in added['ipaddr']:
                            self['ipaddr'].add(i)
                # 8<-----------------------------------------
                ret = self.commit(transaction=snapshot, rollback=True)
                # if some error was returned by the internal
                # closure, substitute the initial one
                if ret is not None:
                    error = ret
                else:
                    error = e
            elif isinstance(e, NetlinkError) and getattr(e, 'code', 0) == 1:
                # It is <Operation not permitted>, catched in
                # rollback. So return it -- see ~5 lines above
                return e
            else:
                # somethig went wrong during automatic rollback.
                # that's the worst case, but it is still possible,
                # since we have no locks on OS level.
                self.drop()
                self['ipaddr'].set_target(None)
                self['ports'].set_target(None)
                # reload all the database -- it can take a long time,
                # but it is required since we have no idea, what is
                # the result of the failure
                #
                # ACHTUNG: database reload is asynchronous, so after
                # getting RuntimeError() from commit(), take a seat
                # and rest for a while. It is an extremal case, it
                # should not became at all, and there is no sync.
                self.nl.get_links()
                self.nl.get_addr()
                x = RuntimeError()
                x.cause = e
                raise x

        # if it is not a rollback turn
        if not rollback:
            # drop last transaction in any case
            self.drop()
            # re-load interface information
            self.reload()

        # raise exception for failed transaction
        if error is not None:
            error.transaction = transaction
            raise error

    def up(self):
        '''
        Shortcut: change interface state to 'up'.

        Requres commit.
        '''
        self['flags'] |= 1

    def down(self):
        '''
        Shortcut: change interface state to 'down'.

        Requires commit.
        '''
        self['flags'] &= ~(self['flags'] & 1)

    def remove(self):
        self['removal'] = True


class IPDB(Dotkeys):
    '''
    The class that maintains information about network setup
    of the host. Monitoring netlink events allows it to react
    immediately. It uses no polling.

    No methods of the class should be called directly.
    '''

    def __init__(self, nl=None, host=None, mode='implicit',
                 key=None, cert=None, ca=None, iclass=Interface):
        '''
        Parameters:
            * nl -- IPRoute() reference

        If you do not provide iproute instance, ipdb will
        start it automatically. Please note, that there can
        be only one iproute instance per process. Actually,
        you can start two and more iproute instances, but
        only the first one will receive anything.
        '''
        self.nl = nl or IPRoute(host=host, key=key, cert=cert, ca=ca)
        self.mode = mode
        self._stop = False
        self.iclass = iclass

        # resolvers
        self.by_name = Dotkeys()
        self.by_index = Dotkeys()

        # caches
        self.ipaddr = {}
        self.routes = {}
        self.neighbors = {}
        self.old_names = {}

        # update events
        self._links_event = threading.Event()

        # load information on startup
        links = self.nl.get_links()
        self.update_links(links)
        self.update_slaves(links)
        self.update_addr(self.nl.get_addr())

        # start monitoring thread
        self.nl.mirror()
        self._mthread = threading.Thread(target=self.monitor)
        self._mthread.setDaemon(True)
        self._mthread.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()

    def __dir__(self):
        ret = Dotkeys.__dir__(self)
        ret.append('by_name')
        ret.append('by_index')
        return ret

    def release(self):
        '''
        Shutdown monitoring thread and release iproute.
        '''
        self._stop = True
        self.nl.get_links()
        self.nl.release()
        self._mthread.join()

    def create(self, kind, ifname, **kwarg):
        '''
        Create an interface. Arguments 'kind' and 'ifname' are
        required.

        * kind -- interface type, can be of:
          * bridge
          * bond
          * vlan
          * tun
          * dummy
        * ifname -- interface name

        Different interface kinds can require different
        arguments for creation.

        FIXME: this should be documented.
        '''
        i = self.iclass(nl=self.nl, ipdb=self, mode='snapshot')
        i['kind'] = kind
        i['index'] = kwarg.get('index', 0)
        i['ifname'] = ifname
        self.by_name[i['ifname']] = self[i['ifname']] = i
        i.update(kwarg)
        i._mode = self.mode
        i.begin()
        return i

    def detach(self, item):
        if item in self:
            del self[item]

    def update_links(self, links):
        '''
        Rebuild links index from list of RTM_NEWLINK messages.
        '''
        for dev in links:
            if dev['index'] not in self.ipaddr:
                self.ipaddr[dev['index']] = LinkedSet()
            i = \
                self.by_index[dev['index']] = \
                self[dev['index']] = \
                self.get(dev.get_attr('IFLA_IFNAME'), None) or \
                self.iclass(nl=self.nl, ipdb=self, mode=self.mode)
            i.load(dev)
            self[i['ifname']] = \
                self.by_name[i['ifname']] = i
            self.old_names[dev['index']] = i['ifname']

    def _lookup_master(self, msg):
        index = msg['index']
        master = msg.get_attr('IFLA_MASTER') or msg.get_attr('IFLA_LINK')
        if _ANCIENT_PLATFORM:
            # FIXME: do something with it, please
            # if the master is not reported by netlink, lookup it
            # through /sys:
            try:
                f = open('/sys/class/net/%s/brport/bridge/ifindex' %
                         (self[index]['ifname']), 'r')
            except IOError:
                return
            master = int(f.read())
            f.close()
            self[index].set_item('master', master)
        elif master:
            master = master
        else:
            master = None

        return self.get(master, None)

    def update_slaves(self, links):
        '''
        Update slaves list -- only after update IPDB!
        '''
        for msg in links:
            master = self._lookup_master(msg)
            # there IS a master for the interface
            if master is not None:
                index = msg['index']
                if msg['event'] == 'RTM_NEWLINK':
                    # TODO tags: ipdb
                    # The code serves one particular case, when
                    # an enslaved interface is set to belong to
                    # another master. In this case there will be
                    # no 'RTM_DELLINK', only 'RTM_NEWLINK', and
                    # we can end up in a broken state, when two
                    # masters refers to the same slave
                    for device in self.by_index:
                        if index in self[device]['ports']:
                            self[device].del_port(index, direct=True)
                    master.add_port(index, direct=True)
                elif msg['event'] == 'RTM_DELLINK':
                    if index in master['ports']:
                        master.del_port(index, direct=True)
            # there is NO masters for the interface, clean them if any
            else:
                device = self[msg['index']]
                master = device.if_master
                if master is not None:
                    if 'master' in device:
                        device.del_item('master')
                    if 'link' in device:
                        device.del_item('link')
                    if (master in self) and \
                            (msg['index'] in self[master].ports):
                        self[master].del_port(msg['index'], direct=True)

    def update_addr(self, addrs, action='add'):
        '''
        Update interface list of an interface.
        '''
        for addr in addrs:
            nla = get_addr_nla(addr)
            if nla is not None:
                method = getattr(self.ipaddr[addr['index']], action)
                try:
                    method(key=(nla, addr['prefixlen']), raw=addr)
                except:
                    pass

    def monitor(self):
        '''
        Main monitoring cycle. It gets messages from the
        default iproute queue and updates objects in the
        database.
        '''
        while not self._stop:
            try:
                messages = self.nl.get()
            except:
                continue
            for msg in messages:
                if msg.get('event', None) == 'RTM_NEWLINK':
                    index = msg['index']
                    if index in self:
                        # get old name
                        old = self.old_names[index]
                        # load interface from the message
                        self[index].load(msg)
                        # check for new name
                        if self[index]['ifname'] != old:
                            # FIXME catch exception
                            # FIXME isolate dict updates
                            del self[old]
                            del self.by_name[old]
                            if index in self.old_names:
                                del self.old_names[index]
                            self[self[index]['ifname']] = self[index]
                            self.by_name[self[index]['ifname']] = self[index]
                            self.old_names[index] = self[index]['ifname']
                    else:
                        self.update_links([msg])
                    self.update_slaves([msg])
                    # what about removal?
                    self._links_event.set()
                elif msg.get('event', None) == 'RTM_DELLINK':
                    self.update_slaves([msg])
                    if msg['change'] == 0xffffffff:
                        # FIXME catch exception
                        self[msg['index']].sync()
                        del self.by_name[self[msg['index']]['ifname']]
                        del self.by_index[msg['index']]
                        del self.old_names[msg['index']]
                        del self[self[msg['index']]['ifname']]
                        del self[msg['index']]
                elif msg.get('event', None) == 'RTM_NEWADDR':
                    self.update_addr([msg], 'add')
                elif msg.get('event', None) == 'RTM_DELADDR':
                    self.update_addr([msg], 'remove')
