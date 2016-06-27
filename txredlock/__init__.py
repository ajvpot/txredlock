import string
import random
import time
from collections import namedtuple

import txredisapi as redis
from redis.exceptions import RedisError

# Python 3 compatibility
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue, DeferredList, Deferred

string_type = getattr(__builtins__, 'basestring', str)

try:
	basestring
except NameError:
	basestring = str

Lock = namedtuple("Lock", ("validity", "resource", "key"))


class CannotObtainLock(Exception):
	pass

def tsleep(secs):
	d = Deferred()
	reactor.callLater(secs, d.callback, None)
	return d

class MultipleRedlockException(Exception):
	def __init__(self, errors, *args, **kwargs):
		super(MultipleRedlockException, self).__init__(*args, **kwargs)
		self.errors = errors

	def __str__(self):
		return ' :: '.join([str(e) for e in self.errors])

	def __repr__(self):
		return self.__str__()


class Redlock(object):
	default_retry_count = 3
	default_retry_delay = 0.2
	clock_drift_factor = 0.01
	unlock_script = """
    if redis.call("get",KEYS[1]) == ARGV[1] then
        return redis.call("del",KEYS[1])
    else
        return 0
    end"""

	def __init__(self, connection_list, retry_count=None, retry_delay=None):
		self.connection_list = connection_list
		self.retry_count = retry_count or self.default_retry_count
		self.retry_delay = retry_delay or self.default_retry_delay

	def connect(self):
		self.servers = []
		serverDeferreds = []
		for connection_info in self.connection_list:
			try:
				if type(connection_info) == dict:
					def addServer(res):
						self.servers.append(res)
						return res
					d = redis.Connection(**connection_info)
					d.addCallback(addServer)
					serverDeferreds.append(d)
				else:
					server = connection_info
					self.servers.append(server)
			except Exception as e:
				raise Warning(str(e))

		def checkQuorun(res):
			self.quorum = (len(self.connection_list) // 2) + 1
			if len(self.servers) < self.quorum:
				raise CannotObtainLock(
					"Failed to connect to the majority of redis servers")
			return res
		dl = DeferredList(serverDeferreds)
		dl.addCallback(checkQuorun)
		return dl

	def lock_instance(self, server, resource, val, ttl):
		try:
			assert isinstance(ttl, int), 'ttl {} is not an integer'.format(ttl)
		except AssertionError as e:
			raise ValueError(str(e))
		return server.set(resource, val, only_if_not_exists=True, pexpire=ttl)

	@inlineCallbacks
	def unlock_instance(self, server, resource, val):
		try:
			yield server.eval(self.unlock_script, (resource), (val))
		except Exception as e:
			print "Error unlocking resource %s in server %s", resource, str(server)

	def get_unique_id(self):
		CHARACTERS = string.ascii_letters + string.digits
		return ''.join(random.choice(CHARACTERS) for _ in range(22)).encode()

	@inlineCallbacks
	def lock(self, resource, ttl):
		retry = 0
		val = self.get_unique_id()

		# Add 2 milliseconds to the drift to account for Redis expires
		# precision, which is 1 millisecond, plus 1 millisecond min
		# drift for small TTLs.
		drift = int(ttl * self.clock_drift_factor) + 2

		redis_errors = list()
		while retry < self.retry_count:
			n = 0
			start_time = int(time.time() * 1000)
			del redis_errors[:]
			for server in self.servers:
				try:
					if (yield self.lock_instance(server, resource, val, ttl)):
						n += 1
				except RedisError as e:
					redis_errors.append(e)
			elapsed_time = int(time.time() * 1000) - start_time
			validity = int(ttl - elapsed_time - drift)
			if validity > 0 and n >= self.quorum:
				if redis_errors:
					raise MultipleRedlockException(redis_errors)
				returnValue(Lock(validity, resource, val))
			else:
				for server in self.servers:
					try:
						yield self.unlock_instance(server, resource, val)
					except:
						pass
				retry += 1
				yield tsleep(self.retry_delay)
		returnValue(False)

	@inlineCallbacks
	def unlock(self, lock):
		redis_errors = []
		for server in self.servers:
			try:
				yield self.unlock_instance(server, lock.resource, lock.key)
			except RedisError as e:
				redis_errors.append(e)
		if redis_errors:
			raise MultipleRedlockException(redis_errors)