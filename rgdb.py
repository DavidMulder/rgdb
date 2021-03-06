#!/usr/bin/python

# The MIT License (MIT)
# 
# Copyright (c) 2014 David Mulder
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import paramiko, sys, time, re, zmq, os, readline, random, signal, getpass, pickle, tempfile, argparse, pty, errno
from functools import wraps
from subprocess import Popen, PIPE

# http://stackoverflow.com/questions/2281850/timeout-function-if-it-takes-too-long-to-finish
class TimeoutError(Exception):
    pass

def timeout(seconds=10, error_message=os.strerror(errno.ETIME)):
    def decorator(func):
        def _handle_timeout(signum, frame):
            raise TimeoutError(error_message)

        def wrapper(*args, **kwargs):
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return result

        return wraps(func)(wrapper)

    return decorator

class gdb:
    def __init__(self, args):
        self.args = args
        self.debugger = 'gdb'
        self.uname = self.__exec__('uname').strip()
        if self.uname == 'Darwin':
            self.debugger = 'lldb'
        self.program_output = '/tmp/rgdb_%s' % time.time()
        signal.signal(signal.SIGINT, self.stop)
        self.binary = 'None' if not self.args else self.args[0] if self.__host_file_exists__(self.args[0]) else 'None'

    def __connect__(self):
        self.__wait_gdb__()
        # Disable hardware watch points (use software watchpoints instead)
        if self.debugger == 'gdb':
            self.__send__('set can-use-hw-watchpoints 0\n')
            self.__wait_gdb__()

    def retrieve_line_number(self):
        line_num = re.findall('Line (\d+) of', self.send(['info', 'line']))
        if len(line_num) == 1:
            line_num = line_num[0].strip()
        else:
            return None
        return line_num

    def __wait_gdb__(self):
        data = ''
        while not any([stop in data.strip().split('\n')[-1] for stop in ['(%s)' % self.debugger, '(y or n)', '(y or [n])']]):
            if self.__recv_ready__():
                data += self.__recv__()
            time.sleep(.1)
        if '(y or n)' in data or '(y or [n])' in data.strip().split('\n')[-1]:
            self.__send__('y\n')
            return '\n'.join(data.split('\n')[1:-1]) + self.__wait_gdb__()
        else:
            return '\n'.join(data.split('\n')[1:-1]).strip()

    def send(self, command):
        # Pipe the output of the program into a file. This resolves some problems parsing out the gdb output vs program output.
        if command[0] == 'run':
            command.append('&>%s' % self.program_output)

        # Modify the command if we're using lldb instead
        if self.debugger == 'lldb':
            if command[0] == 'break':
                command[0] = 'b'
            elif command[0] == 'run':
                command[0] = 'r'
            elif command[0] == 'attach' and re.match('^\d+$', command[1]):
                command[0] = 'attach -p'
            elif command[0] in ['nexti', 'stepi']:
                command[0] = command[0][0] + command[0][-1]
            elif command[0] == 'return':
                command[0] = 'thread return'
            elif command[0] == 'info' and command[1] == 'break':
                command[0] = 'br'
                command[1] = 'l'
            elif command[0] == 'info' and command[1] == 'registers':
                command[0] = 'register'
                command[1] = 'read'
            elif command[0] == 'delete':
                command[0] = 'br del'
            elif command[0] == 'watch':
                command[0] = 'watchpoint set variable'
            elif command[0] == 'x':
                command[0] = 'memory read'
                command[1] = '`%s`' % command[1]
            elif command[0] == 'disassemble':
                command[0] = 'disassemble --frame'
            elif command[0] == 'inspect':
                command[0] = 'p'
        self.__send__('%s\n' % ' '.join(command).strip())
        return self.__wait_gdb__()

    def line(self, command):
        data = self.send(command)

        # Remove the 'Missing separate debuginfo' messages
        if command[0] == 'run':
            data = '\n'.join([line for line in data.split('\n') if not line.startswith('Missing separate debuginfo for') and not line.startswith('Try: ')])

        ending_data = data.replace('\r', '').split('\n\n')[-1].strip()

        # Display the program output which we piped into a file
        program_output = self.__cat_file__(self.program_output)
        if program_output:
            sys.stdout.write('\n... ...\n%s\n... ...\n\n' % program_output)
        self.__blank_file__(self.program_output)

        # Display the gdb output
        print data

    def retrieve_full_path(self, filename):
        location = re.findall('Located in (.*)', self.send(['info', 'source']))
        if len(location) == 1:
            return location[0].strip()
        else:
            return ''

    def stop(self, signal, frame):
        self.close()

@timeout(1) # timeout after 1 second if read() doesn't respond
def timeout_recv(stdout):
    return stdout.read(1)

''' Local gdb connection '''
class lgdb(gdb):
    def __init__(self, args):
        gdb.__init__(self, args)
        self.p = None
        self.stdin = None
        self.stdout = None
        self.host = 'localhost'
        self.__connect__()
        gdb.__connect__(self)

    def __connect__(self):
        master, slave = pty.openpty()
        cmd = [self.debugger]
        if self.args:
            cmd.extend(self.args)
        self.p = Popen(cmd, shell=True, stdin=PIPE, stdout=slave)
        self.stdin = self.p.stdin
        self.stdout = os.fdopen(master)

    def close(self):
        self.stdin.close()
        self.stdout.close()
        exit(1)

    def __send__(self, msg):
        self.stdin.write(msg)

    def __recv_ready__(self):
        return True

    def __recv__(self):
        data = ''
        while True:
            try:
                data += timeout_recv(self.stdout)
            except TimeoutError:
                break
        return data

    def __exec__(self, cmd):
        return Popen(cmd.split(' '), stdout=PIPE).communicate()[0]

    def __host_file_exists__(self, filename):
        return os.path.exists(filename)

    def __blank_file__(self, filename):
        open(filename, 'w').truncate(0)

    def __cat_file__(self, filename):
        return open(filename, 'r').read()

''' Remote gdb connection '''
class rgdb(gdb):
    def __init__(self, args, host, user='root', password=None):
        self.channel = None
        self.socket = None
        self.pid = ''
        self.host = host
        self.location = ''
        self.uname = ''
        self.path_match = {}
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.ssh.connect(self.host, username=user, password=password)
        except:
            try:
                self.ssh.connect(self.host, username=user, password=getpass.getpass('%s\'s password: ' % user))
            except paramiko.ssh_exception.NoValidConnectionsError as e:
                exit(e)
        gdb.__init__(self, args)
        self.channel = self.ssh.invoke_shell()
        self.channel.resize_pty(width=500, height=500)
        self.channel.recv(2048)
        self.channel.send('%s %s\n' % (self.debugger, ' '.join(self.args)))
        gdb.__connect__(self)

    def retrieve_full_path(self, filename):
        location = gdb.retrieve_full_path(self, filename)
        if location in self.path_match:
            return self.path_match[location]
        local_file = '%s/%s_%s' % (tempfile._get_default_tempdir(), next(tempfile._get_candidate_names()), os.path.basename(location))
        sftp = paramiko.SFTPClient.from_transport(self.ssh.get_transport())
        sftp.get(remotepath=location, localpath=local_file)
        self.path_match[location] = local_file
        return local_file

    def close(self):
        for filename in self.path_match.values():
            os.system('rm -f %s' % filename)
        self.__exec__('rm %s' % self.program_output)
        children = self.__exec__('ps -eo pid,command | grep "%s %s" | grep -v grep' % (self.debugger, ' '.join(self.args))).split('\n')
        if len(children) == 1:
            try:
                self.__exec__('kill -9 %s' % children[0].split()[0])
            except:
                pass
        self.ssh.close()
        exit(1)

    def __send__(self, msg):
        self.channel.send(msg)

    def __recv_ready__(self):
        return self.channel.recv_ready()

    def __recv__(self):
        return self.channel.recv(2048)

    def __exec__(self, cmd):
        return self.ssh.exec_command(cmd)[1].read().strip()

    def __host_file_exists__(self, filename):
        return int(self.__exec__('file %s >/dev/null; echo $?' % filename)) == 0

    def __blank_file__(self, filename):
        self.__exec__('>%s' % filename)

    def __cat_file__(self, filename):
        return self.__exec__('cat %s' % filename)

def find_all(name, path, method, tags_file):
    result = []
    for root, dirs, files in os.walk(path, followlinks=True):
        if name in files:
            result.append(os.path.join(root, name))
    if len(result) == 0:
        return None
    elif len(result) == 1:
        return result[0]
    elif method and tags_file:
        matches = []
        for line in open(tags_file, 'r'):
            if line[:line.find(' ')] == method:
                matches.append(line)
        files = []
        for line in matches:
            if '/^' in line:
                files.append(os.path.join(path, line.split('/^')[0].split()[1].strip()))
            else:
                files.append(os.path.join(path, line[:line[:line.index(';')].rfind(' ')].split()[1].strip()))
        result2 = filter(lambda x: x in result, files)
        if len(result2) == 0:
            return select_file(result)
        elif len(result2) == 1:
            return result2[0]
        else:
            return select_file(result2)
    else:
        return select_file(result)

def select_file(result):
    print
    for i in range(0, len(result)):
        print '\t%d:\t%s' % (i, result[i])
    selection = -1
    while selection < 0 or selection > len(result):
        try:
            selection = int(raw_input('Ambiguous file reference, select the correct filename: '))
        except:
            pass
    return result[int(selection)]

ethernet = None
def tcpdump_start(ssh, port):
    global ethernet
    if not ethernet:
        ethers = list(set(ssh.exec_command('netstat -i | cut -d\  -f1 | egrep -v "(Kernel|Iface|Name|lo)"')[1].read().strip().split('\n')))
        if len(ethers) == 1:
            ethernet = ethers[0]
        else:
            for i in range(0, len(ethers)):
                print '\t%d:\t%s' % (i, ethers[i])
            selection = -1
            while selection < 0 or selection > len(ethers):
                try:
                    selection = int(raw_input('Monitor traffic on which ethernet controller? '))
                except:
                    pass
            ethernet = ethers[selection]
    pid = None
    if port:
        pid = ssh.exec_command('echo $$; exec tcpdump -i %s -s0 -w /tmp/tcpdump_debug.out port %s' % (ethernet, port))[1].readline().strip()
    else:
        pid = ssh.exec_command('echo $$; exec tcpdump -i %s -s0 -w /tmp/tcpdump_debug.out' % ethernet)[1].readline().strip()
    time.sleep(2) # give tcpdump a couple seconds to get started
    return pid

def tcpdump_load(ssh, pid):
    time.sleep(1) # give tcpdump time to collect packets
    ssh.exec_command('kill %s' % pid)
    sftp = paramiko.SFTPClient.from_transport(ssh.get_transport())
    try:
        sftp.get(remotepath='/tmp/tcpdump_debug.out', localpath='/tmp/tcpdump_debug.out')
        if os.system('which wireshark  >/dev/null') == 0:
            os.system('wireshark /tmp/tcpdump_debug.out &')
    except IOError:
        pass
    sftp.close()

def debugger(con):
    settings = None
    settings_dir = os.path.join(os.path.expanduser('~'), '.config/rgdb/')
    if not os.path.exists(settings_dir):
        os.makedirs(settings_dir)
    settings_path = os.path.join(settings_dir, 'rgdb_settings')
    if not os.path.exists(settings_path):
        settings = {}
        while not 'code_path' in settings.keys() or not settings['code_path']:
            settings['code_path'] = raw_input('Enter a base path for your code directory: ')
        settings['tags_file'] = raw_input('Enter a tag file path (optional): ')
        settings['reverse'] = True if raw_input('Enable reverse debugging (true/false)? ').lower() == 'true' else False
        pickle.dump(settings, open(settings_path, 'w'))
    else:
        settings = pickle.load(open(settings_path, 'r'))

    debug_id = random.randint(5000, 6000)
    rc = None
    ui = os.path.join(os.path.dirname(sys.argv[0]), 'rgdb_ui')
    if not os.path.exists(ui):
        ui = 'python %s' % os.path.join(os.path.dirname(sys.argv[0]), 'rgdb_ui.py')
    if os.system('which gnome-terminal >/dev/null') == 0:
        rc = os.system('gnome-terminal --title="Remote GDB %s (Editor)" -x %s %d 2>/dev/null' % (con.binary, ui, debug_id))
    else:
        rc = os.system('xterm -T "Remote GDB %s (Editor)" -bg white -fg black -fn 9x15 -e %s %d 2>/dev/null &' % (con.binary, ui, debug_id))
    if rc != 0:
        exit(rc)
    code_path = settings['code_path']
    print 'Debugging %s on %s' % (con.binary, con.host)
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.connect("tcp://127.0.0.1:%d" % debug_id)
    full_specified_name = None
    recent_files = {}
    previous = None
    method = None
    previous_line_num = None
    previous_filename = None
    while True:
        filename = None
        line_num = None

        check_line = False
        try:
            command = raw_input('(rgdb) ').strip().split()
            if not command:
                command = previous
            if not command:
                continue
            if command[0] in ['next', 'step', 'continue', 'finish'] or 'reverse' in command[0]:
                check_line = True
                if len(command) > 1 and command[1] == 'tcpdump' and con.ssh:
                    port = None
                    if len(command) > 2:
                        port = command[2]
                    tcpdump_pid = tcpdump_start(con.ssh, port)
                    con.line([command[0]])
                    tcpdump_load(con.ssh, tcpdump_pid)
                else:
                    con.line(command)
            elif command[0] == 'run':
                check_line = True
                con.line(command)
                if settings['reverse']:
                    con.send('target record-full')
            elif command[0] == 'exit' or command[0] == 'quit':
                con.close()
                break
            else:
                print con.send(command)

            if check_line:
                line_num = con.retrieve_line_number()
                full_path = con.retrieve_full_path(filename)
                if full_path and line_num:
                    if (line_num != previous_line_num and previous_filename == full_path) or previous_filename != full_path:
                        previous_line_num = line_num
                        previous_filename = full_path
                        socket.send("%s:%s" % (full_path, line_num))
                        socket.recv()
            previous = command
        except EOFError:
            print
            socket.send('exit')
            socket.recv()
            socket.close()
            con.close()
            break

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="\tYou\'ll be prompted on first run for a code path and tag file. The code path is where rgdb searches for source code files when it encounters a filename in gdb output. For example, ~/code could be the base directory where you store all your source files.\n\tThe tag file property refers to your ctags file. Having a ctags file improves the speed and accuracy of file searches, but is not required.", formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', '-u', help='Username for authentication')
    parser.add_argument('--password', '-w', help='Password for authentication')
    parser.add_argument('--localhost', '-l', help='Run this command on the localhost', action='store_true')
    parser.add_argument('args', nargs='*', help='If this is a remote call, the first argument must be a hostname for the connection. All preceding arguments will be passed to gdb.')

    args = parser.parse_args()

    if args.user:
        user = args.user
    else:
        user = getpass.getuser()

    if args.localhost:
        con = lgdb(args.args)
    else:
        con = rgdb(args.args[1:], args.args[0], user, args.password)

    debugger(con)

