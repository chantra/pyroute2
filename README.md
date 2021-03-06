pyroute2
========

Pyroute2 is a pure Python netlink and messaging/RPC library.
It requires only Python stdlib, no 3rd party libraries. Later
it can change, but the deps tree will remain as simple, as
it is possible.

The library contains all you need to build either one-node,
or distributed netlink-related solutions. It consists of two
major parts:

* Netlink parsers: NETLINK\_ROUTE, TASKSTATS, etc.
* Messaging infrastructure: broker, clients, etc.

RTNETLINK sample
----------------

More samples you can read in the project documentation.
Low-level interface::

    from pyroute2 import IPRoute

    # get access to the netlink socket
    ip = IPRoute()

    # print interfaces
    print ip.get_links()

    # stop working with netlink and release all sockets
    ip.release()

High-level transactional interface, IPDB::

    from pyroute2 import IPDB
    # local network settings
    ip = IPDB()
    # create bridge and add ports and addresses
    # transaction will be started with `with` statement
    # and will be committed at the end of the block
    with ip.create(kind='bridge', ifname='rhev') as i:
        i.add_port(ip.em1)
        i.add_port(ip.em2)
        i.add_ip('10.0.0.2/24')


The project contains several modules for different types of
netlink messages, not only RTNL.

RPC sample
----------

Actually, either side can act as a server or a client, there
is no pre-defined roles. But for simplicity, they're referred
here as "server" and "client".

Server side::

    from pyroute2.rpc import Node
    from pyroute2.rpc import public


    class Namespace(object):
        '''
        This class acts as a namespace, that contains
        methods to be published via RPC. Any method,
        available through RPC, must be marked as @public.
        '''

        @public
        def echo(self, msg):
            '''
            Simple echo method, that returns a modified
            string.
            '''
            return '%s passed' % (msg)

    # start the RPC node and register the namespace
    node = Node()
    node.register(Namespace())

    # listen for network connections
    node.serve('tcp://localhost:9824')

    # wait for exit -- activity will be done in the
    # background thread
    raw_input(' hit return to exit >> ')

Client side::

    from pyroute2.rpc import Node

    # start the RPC node and connect to the 'server'
    node = Node()
    proxy = node.connect('tcp://localhost:9824')

    # call a remote method through the proxy instance
    print(proxy.echo('test'))


It will print out `test passed`.

installation
------------

`make install` or `pip install pyroute2`

requires
--------

Python >= 2.6

  * test reqs (optional): **python-coverage**, **python-nose**

links
-----

* home: https://github.com/svinota/pyroute2
* bugs: https://github.com/svinota/pyroute2/issues
* pypi: https://pypi.python.org/pypi/pyroute2
* docs: http://peet.spb.ru/pyroute2/
* list: https://groups.google.com/d/forum/pyroute2-dev
