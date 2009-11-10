#!/usr/bin/env python
"""beanstalkc - A beanstalkd Client Library for Python"""

__license__ = '''
Copyright (C) 2008 Andreas Bolka

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

__version__ = '0.1.1'

import socket


DEFAULT_HOST = 'localhost'
DEFAULT_PORT = 11300
DEFAULT_PRIORITY = 2**31
DEFAULT_TTR = 120


class BeanstalkcException(Exception): pass
class UnexpectedResponse(BeanstalkcException): pass
class CommandFailed(BeanstalkcException): pass
class DeadlineSoon(BeanstalkcException): pass


class Connection(object):
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, parse_yaml=True):
        if isinstance(parse_yaml, bool) and parse_yaml == True:
            self.parse_yaml = __import__('yaml').load
        elif hasattr(parse_yaml, '__call__'):
            self.parse_yaml = parse_yaml
        else:
            self.parse_yaml = lambda x: x
        self.host = host
        self.port = port
        self.connect()

    def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))
        self.socket_file = self.socket.makefile('rb')

    def close(self):
        self.socket.close()

    def _interact(self, command, expected_ok, expected_err=[]):
        self.socket.send(command)
        status, results = self._read_response()
        if status in expected_ok:
            return results
        elif status in expected_err:
            raise CommandFailed(command.split()[0], status, results)
        else:
            raise UnexpectedResponse(command.split()[0], status, results)

    def _read_response(self):
        response = self.socket_file.readline().split()
        return response[0], response[1:]

    def _read_body(self, size):
        body = self.socket_file.read(size)
        self.socket_file.read(2) # trailing crlf
        return body

    def _interact_value(self, command, expected_ok, expected_err=[]):
        return self._interact(command, expected_ok, expected_err)[0]

    def _interact_job(self, command, expected_ok, expected_err, reserved=True):
        jid, size = self._interact(command, expected_ok, expected_err)
        body = self._read_body(int(size))
        return Job(self, int(jid), body, reserved)

    def _interact_yaml(self, command, expected_ok, expected_err=[]):
        size, = self._interact(command, expected_ok, expected_err)
        body = self._read_body(int(size))
        return self.parse_yaml(body)

    def _interact_peek(self, command):
        try:
            return self._interact_job(command, ['FOUND'], ['NOT_FOUND'], False)
        except CommandFailed, (_, status, results):
            return None

    # -- public interface --

    def put(self, body, priority=DEFAULT_PRIORITY, delay=0, ttr=DEFAULT_TTR):
        assert isinstance(body, str)
        jid = self._interact_value(
                'put %d %d %d %d\r\n%s\r\n' %
                    (priority, delay, ttr, len(body), body),
                ['INSERTED', 'BURIED'])
        return int(jid)

    def reserve(self, timeout=None):
        if timeout is not None:
            command = 'reserve-with-timeout %d\r\n' % timeout
        else:
            command = 'reserve\r\n'
        try:
            return self._interact_job(command,
                                      ['RESERVED'],
                                      ['DEADLINE_SOON', 'TIMED_OUT'])
        except CommandFailed, (_, status, results):
            if status == 'TIMED_OUT':
                return None
            elif status == 'DEADLINE_SOON':
                raise DeadlineSoon(results)

    def kick(self, bound=1):
        return int(self._interact_value('kick %d\r\n' % bound, ['KICKED']))

    def peek(self, jid):
        return self._interact_peek('peek %d\r\n' % jid)

    def peek_ready(self):
        return self._interact_peek('peek-ready\r\n')

    def peek_delayed(self):
        return self._interact_peek('peek-delayed\r\n')

    def peek_buried(self):
        return self._interact_peek('peek-buried\r\n')

    def tubes(self):
        return self._interact_yaml('list-tubes\r\n', ['OK'])

    def using(self):
        return self._interact_value('list-tube-used\r\n', ['USING'])

    def use(self, name):
        return self._interact_value('use %s\r\n' % name, ['USING'])

    def watching(self):
        return self._interact_yaml('list-tubes-watched\r\n', ['OK'])

    def watch(self, name):
        return int(self._interact_value('watch %s\r\n' % name, ['WATCHING']))

    def ignore(self, name):
        try:
            return int(self._interact_value('ignore %s\r\n' % name,
                                            ['WATCHING'],
                                            ['NOT_IGNORED']))
        except CommandFailed:
            return 1

    def stats(self):
        return self._interact_yaml('stats\r\n', ['OK'])

    def stats_tube(self, name):
        return self._interact_yaml('stats-tube %s\r\n' % name,
                                  ['OK'],
                                  ['NOT_FOUND'])

    # -- job interactors --

    def delete(self, jid):
        self._interact('delete %d\r\n' % jid, ['DELETED'], ['NOT_FOUND'])

    def release(self, jid, priority=None, delay=0):
        self._interact('release %d %d %d\r\n' % (jid, priority, delay),
                       ['RELEASED', 'BURIED'],
                       ['NOT_FOUND'])

    def bury(self, jid, priority=None):
        self._interact('bury %d %d\r\n' % (jid, priority),
                       ['BURIED'],
                       ['NOT_FOUND'])

    def touch(self, jid):
        self._interact('touch %d\r\n' % jid, ['TOUCHED'], ['NOT_FOUND'])

    def stats_job(self, jid):
        return self._interact_yaml('stats-job %d\r\n' % jid,
                                   ['OK'],
                                   ['NOT_FOUND'])


class Job(object):
    def __init__(self, conn, jid, body, reserved=True):
        self.conn = conn
        self.jid = jid
        self.body = body
        self.reserved = reserved

    # -- public interface --

    def delete(self):
        self.conn.delete(self.jid)
        self.reserved = False

    def release(self, priority=None, delay=0):
        if self.reserved:
            self.conn.release(self.jid, priority or self.stats()['pri'], delay)
            self.reserved = False

    def bury(self, priority=None):
        if self.reserved:
            self.conn.bury(self.jid, priority or self.stats()['pri'])
            self.reserved = False

    def touch(self):
        if self.reserved:
            self.conn.touch(self.jid)

    def stats(self):
        return self.conn.stats_job(self.jid)


if __name__ == '__main__':
    import doctest, os, signal
    try:
        pid = os.spawnlp(os.P_NOWAIT,
                         'beanstalkd',
                         'beanstalkd', '-l', '127.0.0.1', '-p', '14711')
        doctest.testfile('TUTORIAL', optionflags=doctest.ELLIPSIS)
    finally:
        os.kill(pid, signal.SIGTERM)
