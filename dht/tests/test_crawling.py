__author__ = 'chris'
from binascii import unhexlify

import dht.constants
import mock
import nacl.signing
import nacl.hash
from txrudp import packet, connection, rudp, constants
from twisted.internet import udp, address, task
from twisted.trial import unittest
from dht.crawling import RPCFindResponse, NodeSpiderCrawl, ValueSpiderCrawl
from dht.node import Node, NodeHeap
from dht.utils import digest
from dht.storage import ForgetfulStorage
from dht.protocol import KademliaProtocol
from protos.objects import Value
from wireprotocol import OpenBazaarProtocol
from db.datastore import Database


class ValueSpiderCrawlTest(unittest.TestCase):
    def setUp(self):
        self.public_ip = '123.45.67.89'
        self.port = 12345
        self.own_addr = (self.public_ip, self.port)
        self.addr1 = ('132.54.76.98', 54321)
        self.addr2 = ('231.76.45.89', 15243)
        self.addr3 = ("193.193.111.00", 99999)

        self.clock = task.Clock()
        connection.REACTOR.callLater = self.clock.callLater

        self.proto_mock = mock.Mock(spec_set=rudp.ConnectionMultiplexer)
        self.handler_mock = mock.Mock(spec_set=connection.Handler)
        self.con = connection.Connection(
            self.proto_mock,
            self.handler_mock,
            self.own_addr,
            self.addr1
        )

        valid_key = "1a5c8e67edb8d279d1ae32fa2da97e236b95e95c837dc8c3c7c2ff7a7cc29855"
        self.signing_key = nacl.signing.SigningKey(valid_key, encoder=nacl.encoding.HexEncoder)
        verify_key = self.signing_key.verify_key
        signed_pubkey = self.signing_key.sign(str(verify_key))
        h = nacl.hash.sha512(signed_pubkey)
        self.storage = ForgetfulStorage()
        self.node = Node(unhexlify(h[:40]), self.public_ip, self.port, signed_pubkey, True)
        self.db = Database(filepath=":memory:")
        self.protocol = KademliaProtocol(self.node, self.storage, 20, self.db)

        self.wire_protocol = OpenBazaarProtocol(self.own_addr)
        self.wire_protocol.register_processor(self.protocol)

        self.protocol.connect_multiplexer(self.wire_protocol)
        self.handler = self.wire_protocol.ConnHandler([self.protocol], self.wire_protocol)

        transport = mock.Mock(spec_set=udp.Port)
        ret_val = address.IPv4Address('UDP', self.public_ip, self.port)
        transport.attach_mock(mock.Mock(return_value=ret_val), 'getHost')
        self.wire_protocol.makeConnection(transport)

        self.node1 = Node(digest("id1"), self.addr1[0], self.addr1[1], digest("key1"), True)
        self.node2 = Node(digest("id2"), self.addr2[0], self.addr2[1], digest("key2"), True)
        self.node3 = Node(digest("id3"), self.addr3[0], self.addr3[1], digest("key3"), True)

    def tearDown(self):
        self.con.shutdown()
        self.wire_protocol.shutdown()

    def test_find(self):
        self._connecting_to_connected()
        self.wire_protocol[self.addr1] = self.con
        self.wire_protocol[self.addr2] = self.con
        self.wire_protocol[self.addr3] = self.con

        self.protocol.router.addContact(self.node1)
        self.protocol.router.addContact(self.node2)
        self.protocol.router.addContact(self.node3)

        node = Node(digest("s"))
        nearest = self.protocol.router.findNeighbors(node)
        spider = ValueSpiderCrawl(self.protocol, node, nearest,
                                  dht.constants.KSIZE, dht.constants.ALPHA)
        spider.find()

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        self.assertEqual(len(self.proto_mock.send_datagram.call_args_list), 4)

    def test_nodesFound(self):
        self._connecting_to_connected()
        self.wire_protocol[self.addr1] = self.con
        self.wire_protocol[self.addr2] = self.con
        self.wire_protocol[self.addr3] = self.con

        self.protocol.router.addContact(self.node1)
        self.protocol.router.addContact(self.node2)
        self.protocol.router.addContact(self.node3)

        # test resonse with uncontacted nodes
        node = Node(digest("s"))
        nearest = self.protocol.router.findNeighbors(node)
        spider = ValueSpiderCrawl(self.protocol, node, nearest, dht.constants.KSIZE, dht.constants.ALPHA)
        response = (True, (self.node1.getProto().SerializeToString(), self.node2.getProto().SerializeToString(),
                           self.node3.getProto().SerializeToString()))
        responses = {self.node1.id: response}
        spider._nodesFound(responses)
        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        self.assertEqual(len(self.proto_mock.send_datagram.call_args_list), 4)

        # test all been contacted
        spider = ValueSpiderCrawl(self.protocol, node, nearest, dht.constants.KSIZE, dht.constants.ALPHA)
        for peer in spider.nearest.getUncontacted():
            spider.nearest.markContacted(peer)
        response = (True, (self.node1.getProto().SerializeToString(), self.node2.getProto().SerializeToString(),
                           self.node3.getProto().SerializeToString()))
        responses = {self.node2.id: response}
        resp = spider._nodesFound(responses)
        self.assertTrue(resp is None)

        # test didn't happen
        spider = ValueSpiderCrawl(self.protocol, node, nearest, dht.constants.KSIZE, dht.constants.ALPHA)
        response = (False, (self.node1.getProto().SerializeToString(), self.node2.getProto().SerializeToString(),
                            self.node3.getProto().SerializeToString()))
        responses = {self.node1.id: response}
        spider._nodesFound(responses)
        self.assertTrue(len(spider.nearest) == 2)

        # test got value
        val = Value()
        val.valueKey = digest("contractID")
        val.serializedData = self.protocol.sourceNode.getProto().SerializeToString()
        response = (True, ("value", val.SerializeToString()))
        responses = {self.node3.id: response}
        spider.nearestWithoutValue = NodeHeap(node, 1)
        value = spider._nodesFound(responses)
        self.assertEqual(value[0], val.SerializeToString())

    def test_handleFoundValues(self):
        self._connecting_to_connected()
        self.wire_protocol[self.addr1] = self.con

        self.protocol.router.addContact(self.node1)
        self.protocol.router.addContact(self.node2)
        self.protocol.router.addContact(self.node3)

        node = Node(digest("s"))
        nearest = self.protocol.router.findNeighbors(node)
        spider = ValueSpiderCrawl(self.protocol, node, nearest, dht.constants.KSIZE, dht.constants.ALPHA)
        val = Value()
        val.valueKey = digest("contractID")
        val.serializedData = self.node1.getProto().SerializeToString()
        val1 = val.SerializeToString()
        value = spider._handleFoundValues([(val1,)])
        self.assertEqual(value[0], val.SerializeToString())

        # test handle multiple values
        val.serializedData = self.node2.getProto().SerializeToString()
        val2 = val.SerializeToString()
        found_values = [(val1,), (val1,), (val2,)]
        self.assertEqual(spider._handleFoundValues(found_values), (val1,))

        # test store value at nearest without value
        spider.nearestWithoutValue.push(self.node1)
        spider._handleFoundValues(found_values)
        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        self.assertTrue(len(self.proto_mock.send_datagram.call_args_list) > 1)
        self.proto_mock.send_datagram.call_args_list = []

    def _connecting_to_connected(self):
        remote_synack_packet = packet.Packet.from_data(
            42,
            self.con.own_addr,
            self.con.dest_addr,
            ack=0,
            syn=True
        )
        self.con.receive_packet(remote_synack_packet)

        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self.next_remote_seqnum = 43

        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_syn_packet = packet.Packet.from_bytes(m_calls[0][0][0])
        seqnum = sent_syn_packet.sequence_number

        self.handler_mock.reset_mock()
        self.proto_mock.reset_mock()

        self.next_seqnum = seqnum + 1


class NodeSpiderCrawlTest(unittest.TestCase):
    def setUp(self):
        self.public_ip = '123.45.67.89'
        self.port = 12345
        self.own_addr = (self.public_ip, self.port)
        self.addr1 = ('132.54.76.98', 54321)
        self.addr2 = ('231.76.45.89', 15243)
        self.addr3 = ("193.193.111.00", 99999)

        self.clock = task.Clock()
        connection.REACTOR.callLater = self.clock.callLater

        self.proto_mock = mock.Mock(spec_set=rudp.ConnectionMultiplexer)
        self.handler_mock = mock.Mock(spec_set=connection.Handler)
        self.con = connection.Connection(
            self.proto_mock,
            self.handler_mock,
            self.own_addr,
            self.addr1
        )

        valid_key = "1a5c8e67edb8d279d1ae32fa2da97e236b95e95c837dc8c3c7c2ff7a7cc29855"
        self.signing_key = nacl.signing.SigningKey(valid_key, encoder=nacl.encoding.HexEncoder)
        verify_key = self.signing_key.verify_key
        signed_pubkey = self.signing_key.sign(str(verify_key))
        h = nacl.hash.sha512(signed_pubkey)
        self.storage = ForgetfulStorage()
        self.node = Node(unhexlify(h[:40]), self.public_ip, self.port, signed_pubkey, True)
        self.db = Database(filepath=":memory:")
        self.protocol = KademliaProtocol(self.node, self.storage, 20, self.db)

        self.wire_protocol = OpenBazaarProtocol(self.own_addr)
        self.wire_protocol.register_processor(self.protocol)

        self.protocol.connect_multiplexer(self.wire_protocol)
        self.handler = self.wire_protocol.ConnHandler([self.protocol], self.wire_protocol)

        transport = mock.Mock(spec_set=udp.Port)
        ret_val = address.IPv4Address('UDP', self.public_ip, self.port)
        transport.attach_mock(mock.Mock(return_value=ret_val), 'getHost')
        self.wire_protocol.makeConnection(transport)

        self.node1 = Node(digest("id1"), self.addr1[0], self.addr1[1], digest("key1"), True)
        self.node2 = Node(digest("id2"), self.addr2[0], self.addr2[1], digest("key2"), True)
        self.node3 = Node(digest("id3"), self.addr3[0], self.addr3[1], digest("key3"), True)

    def test_find(self):
        self._connecting_to_connected()
        self.wire_protocol[self.addr1] = self.con
        self.wire_protocol[self.addr2] = self.con
        self.wire_protocol[self.addr3] = self.con

        self.protocol.router.addContact(self.node1)
        self.protocol.router.addContact(self.node2)
        self.protocol.router.addContact(self.node3)

        node = Node(digest("s"))
        nearest = self.protocol.router.findNeighbors(node)
        spider = NodeSpiderCrawl(self.protocol, node, nearest, 20, 3)
        spider.find()

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        self.assertEqual(len(self.proto_mock.send_datagram.call_args_list), 4)

    def test_nodesFound(self):
        self._connecting_to_connected()
        self.wire_protocol[self.addr1] = self.con
        self.wire_protocol[self.addr2] = self.con
        self.wire_protocol[self.addr3] = self.con

        self.protocol.router.addContact(self.node1)
        self.protocol.router.addContact(self.node2)
        self.protocol.router.addContact(self.node3)

        node = Node(digest("s"))
        nearest = self.protocol.router.findNeighbors(node)
        spider = NodeSpiderCrawl(self.protocol, node, nearest, 20, 3)
        response = (True, (self.node1.getProto().SerializeToString(), self.node2.getProto().SerializeToString(),
                           self.node3.getProto().SerializeToString()))
        responses = {self.node1.id: response}
        spider._nodesFound(responses)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        self.assertEqual(len(self.proto_mock.send_datagram.call_args_list), 4)

        response = (True, (self.node1.getProto().SerializeToString(), self.node2.getProto().SerializeToString(),
                           self.node3.getProto().SerializeToString()))
        responses = {self.node1.id: response}
        nodes = spider._nodesFound(responses)
        node_protos = []
        for n in nodes:
            node_protos.append(n.getProto())

        self.assertTrue(self.node1.getProto() in node_protos)
        self.assertTrue(self.node2.getProto() in node_protos)
        self.assertTrue(self.node3.getProto() in node_protos)

        response = (False, (self.node1.getProto().SerializeToString(), self.node2.getProto().SerializeToString(),
                            self.node3.getProto().SerializeToString()))
        responses = {self.node1.id: response}
        nodes = spider._nodesFound(responses)
        node_protos = []
        for n in nodes:
            node_protos.append(n.getProto())

        self.assertTrue(self.node2.getProto() in node_protos)
        self.assertTrue(self.node3.getProto() in node_protos)

    def _connecting_to_connected(self):
        remote_synack_packet = packet.Packet.from_data(
            42,
            self.con.own_addr,
            self.con.dest_addr,
            ack=0,
            syn=True
        )
        self.con.receive_packet(remote_synack_packet)

        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self.next_remote_seqnum = 43

        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_syn_packet = packet.Packet.from_bytes(m_calls[0][0][0])
        seqnum = sent_syn_packet.sequence_number

        self.handler_mock.reset_mock()
        self.proto_mock.reset_mock()

        self.next_seqnum = seqnum + 1


class RPCFindResponseTest(unittest.TestCase):
    def test_happened(self):
        response = (True, ("value", "some_value"))
        r = RPCFindResponse(response)
        self.assertTrue(r.happened())
        response = (False, ("value", "some_value"))
        r = RPCFindResponse(response)
        self.assertFalse(r.happened())

    def test_hasValue(self):
        response = (True, ("value", "some_value"))
        r = RPCFindResponse(response)
        self.assertTrue(r.hasValue())
        response = (False, "a node")
        r = RPCFindResponse(response)
        self.assertFalse(r.hasValue())

    def test_getValue(self):
        response = (True, ("value", "some_value"))
        r = RPCFindResponse(response)
        self.assertEqual(r.getValue(), ("some_value",))

    def test_getNodeList(self):
        node1 = Node(digest("id1"), "127.0.0.1", 12345, signed_pubkey=digest("key1"), vendor=True)
        node2 = Node(digest("id2"), "127.0.0.1", 22222, signed_pubkey=digest("key2"), vendor=True)
        node3 = Node(digest("id3"), "127.0.0.1", 77777, signed_pubkey=digest("key3"))
        response = (True, (node1.getProto().SerializeToString(), node2.getProto().SerializeToString(),
                           node3.getProto().SerializeToString(),
                           "sdfasdfsd"))
        r = RPCFindResponse(response)
        nodes = r.getNodeList()
        self.assertEqual(nodes[0].getProto(), node1.getProto())
        self.assertEqual(nodes[1].getProto(), node2.getProto())
        self.assertEqual(nodes[2].getProto(), node3.getProto())
