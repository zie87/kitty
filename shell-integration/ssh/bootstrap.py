#!/usr/bin/env python
# License: GPLv3 Copyright: 2022, Kovid Goyal <kovid at kovidgoyal.net>


import atexit
import base64
import io
import os
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import termios

tty_fd = -1
echo_on = int('ECHO_ON')
data_dir = shell_integration_dir = ''
request_data = int('REQUEST_DATA')
leading_data = b''
HOME = os.path.expanduser('~')
login_shell = pwd.getpwuid(os.geteuid()).pw_shell or 'sh'


def cleanup():
    global tty_fd
    if tty_fd > -1:
        if echo_on:
            s = termios.tcgetattr(tty_fd)
            s[3] |= termios.ECHO
            termios.tcsetattr(tty_fd, termios.TCSANOW, s)
        os.close(tty_fd)
        tty_fd = -1


def write_all(fd, data) -> None:
    if isinstance(data, str):
        data = data.encode('utf-8')
    data = memoryview(data)
    while data:
        try:
            n = os.write(fd, data)
        except BlockingIOError:
            continue
        if not n:
            break
        data = data[n:]


def dcs_to_kitty(type, payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    payload = base64.standard_b64encode(payload)
    return b'\033P@kitty-' + type.encode('ascii') + b'|' + payload + b'\033\\'


def send_data_request():
    write_all(tty_fd, dcs_to_kitty(
        'ssh', 'id=REQUEST_ID:pwfile=PASSWORD_FILENAME:pw=DATA_PASSWORD'))


def debug(msg):
    data = dcs_to_kitty('print', 'debug: {}'.format(msg))
    if tty_fd == -1:
        with open(os.ctermid(), 'wb') as fl:
            write_all(fl.fileno(), data)
    else:
        write_all(tty_fd, data)


def unquote_env_val(x):
    return re.sub('\\\\([\\$`\x22\n])', r'\1', x[1:-1])


def apply_env_vars(raw):
    global login_shell

    def process_defn(defn):
        parts = defn.split('=', 1)
        if len(parts) == 1:
            key, val = parts[0], ''
        else:
            key, val = parts
            val = os.path.expandvars(unquote_env_val(val))
        os.environ[key] = val

    for line in raw.splitlines():
        if line.startswith('export '):
            process_defn(line.split(' ', 1)[-1])
        elif line.startswith('unset '):
            os.environ.pop(line.split(' ', 1)[-1], None)
    login_shell = os.environ.pop('KITTY_LOGIN_SHELL', login_shell)


def move(src, base_dest):
    for x in os.scandir(src):
        dest = os.path.join(base_dest, x.name)
        if x.is_dir(follow_symlinks=False):
            os.makedirs(dest, exist_ok=True)
            move(x.path, dest)
        else:
            shutil.move(x.path, dest)


def compile_terminfo(base):
    tic = shutil.which('tic')
    if not tic:
        return
    tname = '.terminfo'
    if os.path.exists('/usr/share/misc/terminfo.cdb'):
        tname += '.cdb'
    os.environ['TERMINFO'] = os.path.join(HOME, tname)
    cp = subprocess.run(
        [tic, '-x', '-o', os.path.join(base, tname), os.path.join(base, '.terminfo', 'kitty.terminfo')],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    if cp.returncode != 0:
        sys.stderr.buffer.write(cp.stdout)
        raise SystemExit('Failed to compile the terminfo database')
    q = os.path.join(base, tname, '78', 'xterm-kitty')
    if not os.path.exists(q):
        os.makedirs(os.path.dirname(q), exist_ok=True)
        os.symlink('../x/xterm-kitty', q)


def iter_base64_data(f):
    global leading_data
    started = 0
    for line in f:
        line = line.rstrip()
        if started == 0:
            if line == b'KITTY_DATA_START':
                started = 1
            else:
                leading_data += line
        elif started == 1:
            if line == b'OK':
                started = 2
            else:
                raise SystemExit(line.decode('utf-8', 'replace').rstrip())
        else:
            if line == b'KITTY_DATA_END':
                break
            yield line


def get_data():
    global data_dir, shell_integration_dir, leading_data
    data = []
    with open(tty_fd, 'rb', closefd=False) as f:
        data = b''.join(iter_base64_data(f))
    if leading_data:
        # clear current line as it might have things echoed on it from leading_data
        # because we only turn off echo in this script whereas the leading bytes could
        # have been sent before the script had a chance to run
        print(end='\r\033[K')
    data = base64.standard_b64decode(data)
    with tempfile.TemporaryDirectory(dir=HOME, prefix='.kitty-ssh-kitten-untar-') as tdir, tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(tdir)
        with open(tdir + '/data.sh') as f:
            env_vars = f.read()
            apply_env_vars(env_vars)
            data_dir = os.path.join(HOME, os.environ.pop('KITTY_SSH_KITTEN_DATA_DIR'))
            shell_integration_dir = os.path.join(data_dir, 'shell-integration')
            compile_terminfo(tdir + '/home')
            move(tdir + '/home', HOME)
            if os.path.exists(tdir + '/root'):
                move(tdir + '/root', '/')


def exec_zsh_with_integration():
    zdotdir = os.environ.get('ZDOTDIR') or ''
    if not zdotdir:
        zdotdir = HOME
        os.environ.pop('KITTY_ORIG_ZDOTDIR', None)  # ensure this is not propagated
    else:
        os.environ['KITTY_ORIG_ZDOTDIR'] = zdotdir
    # dont prevent zsh-newuser-install from running
    for q in ('.zshrc', '.zshenv', '.zprofile', '.zlogin'):
        if os.path.exists(os.path.join(HOME, q)):
            os.environ['ZDOTDIR'] = shell_integration_dir + '/zsh'
            os.execlp(login_shell, os.path.basename(login_shell), '-l')
    os.environ.pop('KITTY_ORIG_ZDOTDIR', None)  # ensure this is not propagated


def exec_fish_with_integration():
    if not os.environ.get('XDG_DATA_DIRS'):
        os.environ['XDG_DATA_DIRS'] = shell_integration_dir
    else:
        os.environ['XDG_DATA_DIRS'] = shell_integration_dir + ':' + os.environ['XDG_DATA_DIRS']
    os.environ['KITTY_FISH_XDG_DATA_DIR'] = shell_integration_dir
    os.execlp(login_shell, os.path.basename(login_shell), '-l')


def exec_bash_with_integration():
    os.environ['ENV'] = os.path.join(shell_integration_dir, 'bash', 'kitty.bash')
    os.environ['KITTY_BASH_INJECT'] = '1'
    if not os.environ.get('HISTFILE'):
        os.environ['HISTFILE'] = os.path.join(HOME, '.bash_history')
        os.environ['KITTY_BASH_UNEXPORT_HISTFILE'] = '1'
    os.execlp(login_shell, os.path.basename('login_shell'), '--posix')


def exec_with_shell_integration():
    shell_name = os.path.basename(login_shell).lower()
    if shell_name == 'zsh':
        exec_zsh_with_integration()
    if shell_name == 'fish':
        exec_fish_with_integration()
    if shell_name == 'bash':
        exec_bash_with_integration()


def main():
    global tty_fd, login_shell
    tty_fd = os.open(os.ctermid(), os.O_RDWR | os.O_CLOEXEC)
    try:
        send_data_request()
        get_data()
    finally:
        cleanup()
    cwd = os.environ.pop('KITTY_LOGIN_CWD', '')
    if cwd:
        os.chdir(cwd)
    ksi = frozenset(filter(None, os.environ.get('KITTY_SHELL_INTEGRATION', '').split()))
    exec_cmd = b'EXEC_CMD'
    if exec_cmd:
        cmd = base64.standard_b64decode(exec_cmd).decode('utf-8')
        os.execlp(login_shell, os.path.basename(login_shell), '-c', cmd)
    TEST_SCRIPT  # noqa
    if ksi and 'no-rc' not in ksi:
        exec_with_shell_integration()
    os.environ.pop('KITTY_SHELL_INTEGRATION', None)
    os.execlp(login_shell, '-' + os.path.basename(login_shell))


atexit.register(cleanup)
main()