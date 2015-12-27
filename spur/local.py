import os
import sys
import subprocess
import shutil
io = __import__("io")
import threading
import errno

try:
    import pty
except ImportError:
    pty = None

from spur.tempdir import create_temporary_dir
from spur.files import FileOperations
import spur.results
from .io import IoHandler, Channel
from .errors import NoSuchCommandError


class LocalShell(object):
    def __enter__(self):
        return self
        
    def __exit__(self, *args):
        pass
    
    def upload_dir(self, source, dest, ignore=None):
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*ignore))

    def upload_file(self, source, dest):
        shutil.copyfile(source, dest)
    
    def open(self, name, mode="r"):
        return open(name, mode)
    
    def write_file(self, remote_path, contents):
        subprocess.check_call(["mkdir", "-p", os.path.dirname(remote_path)])
        open(remote_path, "w").write(contents)

    def spawn(self, command, *args, **kwargs):
        stdout = kwargs.pop("stdout", None)
        stderr = kwargs.pop("stderr", None)
        allow_error = kwargs.pop("allow_error", False)
        store_pid = kwargs.pop("store_pid", False)
        use_pty = kwargs.pop("use_pty", False)
        if use_pty:
            if pty is None:
                raise ValueError("use_pty is not supported when the pty module cannot be imported")
            master, slave = pty.openpty()
            stdin_arg = slave
            stdout_arg = slave
            stderr_arg = subprocess.STDOUT
        else:
            stdin_arg = subprocess.PIPE
            stdout_arg = subprocess.PIPE
            stderr_arg = subprocess.PIPE
                
        try:
            process = subprocess.Popen(
                stdin=stdin_arg,
                stdout=stdout_arg,
                stderr=stderr_arg,
                bufsize=0,
                **self._subprocess_args(command, *args, **kwargs)
            )
        except OSError as error:
            if self._is_no_such_command_oserror(error, command[0]):
                raise NoSuchCommandError(command[0])
            else:
                raise
            
        if use_pty:
            # TODO: Should close master ourselves rather than relying on
            # garbage collection
            process_stdin = os.fdopen(os.dup(master), "wb", 0)
            process_stdout = os.fdopen(master, "rb", 0)
            process_stderr = io.BytesIO()
            
            def close_slave_on_exit():
                process.wait()
                # TODO: ensure the IO handler has finished before closing
                os.close(slave)
            
            thread = threading.Thread(target=close_slave_on_exit)
            thread.daemon = True
            thread.start()
            
        else:
            process_stdin = process.stdin
            process_stdout = process.stdout
            process_stderr = process.stderr
            
        spur_process = LocalProcess(
            process,
            allow_error=allow_error,
            process_stdin=process_stdin,
            channels=[
                Channel(process_stdout, stdout, is_pty=use_pty),
                Channel(process_stderr, stderr, is_pty=use_pty),
            ]
        )
        if store_pid:
            spur_process.pid = process.pid
        return spur_process
    
    def run(self, *args, **kwargs):
        return self.spawn(*args, **kwargs).wait_for_result()
        
    def temporary_dir(self):
        return create_temporary_dir()
    
    @property
    def files(self):
        return FileOperations(self)
    
    def _subprocess_args(self, command, cwd=None, update_env=None, new_process_group=False):
        kwargs = {
            "args": command,
            "cwd": cwd,
        }
        if update_env is not None:
            new_env = os.environ.copy()
            new_env.update(update_env)
            kwargs["env"] = new_env
        if new_process_group:
            kwargs["preexec_fn"] = os.setpgrp
        return kwargs
    
    def _is_no_such_command_oserror(self, error, command):
        if error.errno != errno.ENOENT:
            return False
        if sys.version_info[0] < 3:
            return error.filename is None
        else:
            # In Python 3, filename and filename2 are None both when
            # the command and cwd don't exist, but in both cases,
            # the repr of the non-existent path is appended to the
            # error message
            return error.args[1] == os.strerror(error.errno) + ": " + repr(command)
            

class LocalProcess(object):
    def __init__(self, subprocess, allow_error, process_stdin, channels):
        self._subprocess = subprocess
        self._allow_error = allow_error
        self._process_stdin = process_stdin
        self._result = None
            
        self._io = IoHandler(channels)
        
    def is_running(self):
        return self._subprocess.poll() is None
        
    def stdin_write(self, value):
        self._process_stdin.write(value)
        
    def send_signal(self, signal):
        self._subprocess.send_signal(signal)
        
    def wait_for_result(self):
        if self._result is None:
            self._result = self._generate_result()
            
        return self._result
        
    def _generate_result(self):
        output, stderr_output = self._io.wait()
        return_code = self._subprocess.wait()
        
        return spur.results.result(
            return_code,
            self._allow_error,
            output,
            stderr_output
        )

