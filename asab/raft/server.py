import random
import re
import os
import socket
import logging

import asab

from .rpc import RPCMethod, RPCResult
from .server_states import FollowerState, CandidateState, LeaderState
from .server_peer import Peer

#

L = logging.getLogger(__name__)

#


class RaftServer(object):


	def __init__(self, app, rpc):
		self.Loop = app.Loop
		self.State = None

		self.RPC = rpc
		self.RPC.bind(self)

		self.Id = asab.Config["asab:raft"]["server_id"]
		if self.Id == "" or self.Id is None:
			self.Id = "{}:{}".format(socket.gethostname(), rpc.PrimarySocket.getsockname()[1])

		self.ElectionTimerRange = (
			asab.Config["asab:raft"].getint("election_timeout_min"),
			asab.Config["asab:raft"].getint("election_timeout_max")
		)
		assert(self.ElectionTimerRange[0] < self.ElectionTimerRange[1])
		self.ElectionTimer = asab.Timer(self._on_election_timeout, loop=self.Loop)

		self.HeartBeatTimeout = asab.Config["asab:raft"].getint("heartbeat_timeout") / 1000.0
		self.HeartBeatTimer = asab.Timer(self._on_heartbeat_timeout, autorestart=True, loop=self.Loop)

		var_dir = asab.Config['general']['var_dir']
		self.PersistentState = asab.PersistentDict(os.path.join(var_dir, '{}.raft'.format(self.Id.replace('.','-'))))
		self.PersistentState.setdefault('currentTerm', 0)
		self.PersistentState.setdefault('votedFor', None)
		self.PersistentState.setdefault('log', [])

		self.VolatileState = {
			'commitIndex': 0,
			'lastApplied': 0,
		}

		self.Peers = []

		# Add self to peers
		p = Peer(None)
		p.Id = self.Id
		p.RPCdue = 0.0
		self.Peers.append(p)

		# Parse peers
		ps = asab.Config["asab:raft"]["peers"]
		for p in ps.split('\n'):
			p = p.strip()
			if len(p) == 0: continue
			addr, port = p.split(' ', 1)
			port = int(port)
			addr = addr.strip()

			# Try to detect 'self' among peers
			if (addr == 'localhost') or re.match(r'^127\.0+\.0+\.\d$', addr) or (addr == "::1"):
				for s in rpc.Sockets:
					if (port == rpc.PrimarySocket.getsockname()[1]):
						# Skip this peer entry ...
						addr = None

			if addr is not None:
				self.Peers.append(Peer((addr, port)))

		assert(len(self.Peers) > 0)


	async def initialize(self, app):
		# Enter follower state
		self.State = FollowerState(self)


	async def finalize(self, app):
		self.ElectionTimer.stop()
		self.HeartBeatTimer.stop()

	#

	def get_election_timeout(self):
		'''
		Get randomized election timeout in miliseconds
		'''
		return random.randint(*self.ElectionTimerRange) / 1000.0	

	#

	async def _on_election_timeout(self):
		self.State = CandidateState(self)


	async def _on_heartbeat_timeout(self):
		self.State.on_heartbeat_timeout(self)


	def evalute_election(self):
		if isinstance(self.State, LeaderState):
			# Already a leader
			return

		if isinstance(self.State, FollowerState):
			L.warn("We are follower, cannot evaluate election")
			return

		voted_yes = 0
		voted_no = 0
		for peer in self.Peers:
			if peer.VoteGranted:
				voted_yes += 1
			else:
				voted_no += 1

		# A candidate wins an election if it receives votes from a majority of the servers in the full cluster for the same term.
		if voted_yes > voted_no:
			self.State = LeaderState(self)

	#

	def _convert_to_follower(self, term):
		'''
		If term in RPC request or response is higher than current term, convert to follower
		'''
		assert(self.State.CurrentTerm < term)
		self.PersistentState['currentTerm'] = self.State.CurrentTerm = term
		if not isinstance(self.State, FollowerState):
			self.State = FollowerState(self)


	@RPCMethod("AppendEntries")
	def append_entries_server(self, params):
		term = params['term']
		leaderId = params['leaderId']

		ret = {
			'term': self.State.CurrentTerm,
			'success': False,
			'serverId': self.Id,
			'timestamp': params['timestamp'],
		}

		if term > self.State.CurrentTerm:
			L.warn("Current term synced from {} to {}".format(self.State.CurrentTerm, term))
			self._convert_to_follower(term)
			ret['term'] = term

		elif term == self.State.CurrentTerm:
			pass

		else:
			L.warning("Received AppendEntries for an old term:{} when current term is {}".format(term, self.State.CurrentTerm))
			return ret

		if isinstance(self.State, FollowerState):
			self.ElectionTimer.restart(self.get_election_timeout())

		else:
			self.State = FollowerState(self)

		ret['success'] = True
		return ret


	@RPCResult("AppendEntries")
	def append_entries_result(self, peer_address, params):
		# Dispatch RPC result to state object
		if isinstance(self.State, LeaderState):
			self.State.append_entries_result(self, peer_address, params)
		else:
			L.warn("Received AppendEntries result when not leader but {}".format(self.State))

	#

	@RPCMethod("RequestVote")
	def request_vote_server(self, params):
		term = params['term']
		candidateId = params['candidateId']

		votedFor = self.PersistentState['votedFor']

		ret = {
			'term': term,
			'voteGranted': False,
			'serverId': self.Id,
			'timestamp': params['timestamp'],
		}

		if term < self.State.CurrentTerm:
			# An older term received
			return ret

		elif term > self.State.CurrentTerm:
			#if RPC request contains term higher than currentTerm, convert to follower and set the term
			self._convert_to_follower(term)
			self.PersistentState['votedFor'] = votedFor = candidateId
			ret['voteGranted'] = True
			L.warn("Voted for '{}' in {} term (higher term)".format(candidateId, term))

		else: # term == self.State.CurrentTerm

			if (votedFor is not None) and (votedFor != candidateId):
				# We voted for someone else
				return ret

			elif (votedFor is None) or (votedFor == candidateId):
				#TODO: Also check that candidate log is at least as up-to-date as receiver's log

				ret['voteGranted'] = True
				if votedFor is None:
					self.PersistentState['votedFor'] = candidateId
					L.warn("Voted for '{}' in {} term (not voted)".format(candidateId, term))
				else:
					L.warn("Voted for '{}' in {} term (confirm vote)".format(candidateId, term))

				if isinstance(self.State, CandidateState):
					self.State = FollowerState(self)
				else:
					assert(isinstance(self.State, FollowerState))
					self.ElectionTimer.restart(self.get_election_timeout())

		return ret


	@RPCResult("RequestVote")
	def request_vote_result(self, peer_address, params):
		# Dispatch RPC result to state object
		if isinstance(self.State, CandidateState):
			self.State.request_vote_result(self, peer_address, params)
		else:
			L.warn("Received AppendEntries result when not candidate but {}".format(self.State))
