# Copyright 2014 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A libusb1-based ADB reimplementation.

ADB was giving us trouble with its client/server architecture, which is great
for users and developers, but not so great for reliable scripting. This will
allow us to more easily catch errors as Python exceptions instead of checking
random exit codes, and all the other great benefits from not going through
subprocess and a network socket.

All timeouts are in milliseconds.
"""

import io
import os
import socket

from adb import adb_protocol
from adb import common
from adb import filesync_protocol

# From adb.h
CLASS = 0xFF
SUBCLASS = 0x42
PROTOCOL = 0x01
# pylint: disable=invalid-name
DeviceIsAvailable = common.InterfaceMatcher(CLASS, SUBCLASS, PROTOCOL)


try:
  # Imported locally to keep compatibility with previous code.
  from adb.sign_m2crypto import M2CryptoSigner
except ImportError:
  # Ignore this error when M2Crypto is not installed, there are other options.
  pass


class AdbCommands(object):
  """Exposes adb-like methods for use.

  Some methods are more-pythonic and/or have more options.
  """
  protocol_handler = adb_protocol.AdbMessage
  filesync_handler = filesync_protocol.FilesyncProtocol

  def __init__(self):

    self.__reset()

  def __reset(self):
    self.handle = None
    self._device_state = None

    # Connection table tracks each open AdbConnection objects per service type
    # By default, the only service connections that make sense to hold open is (interactive) shell
    self._service_connections = {
        b'shell:': None
    }

  def _get_service_connection(self, service, service_command=None, create=True, timeout_ms=None):
    """
    Based on the service, get the AdbConnection for that service or create one if it doesnt exist

    :param service:
    :param service_command: Additional service parameters to append
    :param create: If False, dont create a connection if it does not exist
    :return:
    """

    connection = self._service_connections.get(service, None)

    if connection:
      return connection

    if not connection and not create:
      return None

    if service_command:
      destination_str = b'%s:%s' % (service, service_command)
    else:
      destination_str = service

    connection = self.protocol_handler.Open(
      self.handle, destination=destination_str, timeout_ms=timeout_ms)

    self._service_connections.update({service: connection})

    return connection

  def ConnectDevice(self, port_path=None, serial=None, default_timeout_ms=None, **kwargs):
    """Convenience function to get an adb device from usb path or serial.

    Args:
      port_path: The filename of usb port to use.
      serial: The serial number of the device to use.
      default_timeout_ms: The default timeout in milliseconds to use.

    If serial specifies a TCP address:port, then a TCP connection is
    used instead of a USB connection.
    """
    if serial and b':' in serial:
        self.handle = common.TcpHandle(serial, timeout_ms=default_timeout_ms)
    else:
        self.handle = common.UsbHandle.FindAndOpen(
            DeviceIsAvailable, port_path=port_path, serial=serial,
            timeout_ms=default_timeout_ms)

    self.__Connect(**kwargs)

    return self

  def Close(self):
    for conn in list(self._service_connections.values()):
      if conn:
        try:
          conn.Close()
        except:
          pass

    self.handle.Close()
    self.__reset()

  def __Connect(self, banner=None, **kwargs):
    """Connect to the device.

    Args:
      usb: UsbHandle or TcpHandle instance to use.
      banner: See protocol_handler.Connect.
      **kwargs: See protocol_handler.Connect for kwargs. Includes rsa_keys,
          and auth_timeout_ms.
    Returns:
      An instance of this class if the device connected successfully.
    """
    if not banner:
      banner = socket.gethostname().encode()
      conn_str = self.protocol_handler.Connect(self.handle, banner=banner, **kwargs)
    # Remove banner and colons after device state (state::banner)
    parts = conn_str.split(b'::')
    device_state = parts[0]

    # Break out the build prop info
    build_props = str(parts[1].split(b';'))
    # print("Device build props: {}".format(build_props))

    self._device_state = device_state

    return True

  @classmethod
  def Devices(cls):
    """Get a generator of UsbHandle for devices available."""
    return common.UsbHandle.FindDevices(DeviceIsAvailable)

  def GetState(self):
    return self._device_state

  def Install(self, apk_path, destination_dir='', replace_existing=True, timeout_ms=None):
    """Install an apk to the device.

    Doesn't support verifier file, instead allows destination directory to be
    overridden.

    Args:
      apk_path: Local path to apk to install.
      destination_dir: Optional destination directory. Use /system/app/ for
        persistent applications.
      replace_existing: whether to replace existing application
      timeout_ms: Expected timeout for pushing and installing.

    Returns:
      The pm install output.
    """
    if not destination_dir:
      destination_dir = '/data/local/tmp/'
    basename = os.path.basename(apk_path)
    destination_path = os.path.join(destination_dir, basename)
    self.Push(apk_path, destination_path, timeout_ms=timeout_ms)

    cmd = ['pm install']
    if replace_existing:
      cmd.append('-r')
    cmd.append('"{}"'.format(destination_path))

    return self.Shell(' '.join(cmd), timeout_ms=timeout_ms)

  def Uninstall(self, package_name, keep_data=False, timeout_ms=None):
    """Removes a package from the device.

    Args:
      package_name: Package name of target package.
      keep_data: whether to keep the data and cache directories
      timeout_ms: Expected timeout for pushing and installing.

    Returns:
      The pm uninstall output.
    """
    cmd = ['pm uninstall']
    if keep_data:
      cmd.append('-k')
    cmd.append('"%s"' % package_name)
    return self.Shell(' '.join(cmd), timeout_ms=timeout_ms)

  def Push(self, source_file, device_filename, mtime='0', timeout_ms=None):
    """Push a file or directory to the device.

    Args:
      source_file: Either a filename, a directory or file-like object to push to
                   the device.
      device_filename: Destination on the device to write to.
      mtime: Optional, modification time to set on the file.
      timeout_ms: Expected timeout for any part of the push.
    """
    if isinstance(source_file, str):
      if os.path.isdir(source_file):
        self.Shell("mkdir " + device_filename)
        for f in os.listdir(source_file):
          self.Push(os.path.join(source_file, f), device_filename + '/' + f)
        return
      source_file = open(source_file, 'rb')

    with source_file:
      connection = self.protocol_handler.Open(
        self.handle, destination=b'sync:', timeout_ms=timeout_ms)
      self.filesync_handler.Push(connection, source_file, device_filename,
                               mtime=int(mtime))
    connection.Close()

  def Pull(self, device_filename, dest_file=None, timeout_ms=None):
    """Pull a file from the device.

    Args:
      device_filename: Filename on the device to pull.
      dest_file: If set, a filename or writable file-like object.
      timeout_ms: Expected timeout for any part of the pull.

    Returns:
      The file data if dest_file is not set. Otherwise, True if the destination file exists
    """
    if not dest_file:
      dest_file = io.BytesIO()
    elif isinstance(dest_file, str):
      dest_file = open(dest_file, 'w')
    else:
      raise ValueError("destfile is of unknown type")

    #conn = self._get_service_connection(b'sync:')
    conn = self.protocol_handler.Open(
        self.handle, destination=b'sync:')

    self.filesync_handler.Pull(conn, device_filename, dest_file)

    conn.Close()
    if isinstance(dest_file, io.BytesIO):
      return dest_file.getvalue()
    else:
      dest_file.close()
      return os.path.exists(dest_file)

  def Stat(self, device_filename):
    """Get a file's stat() information."""
    connection = self.protocol_handler.Open(self.handle, destination=b'sync:')
    mode, size, mtime = self.filesync_handler.Stat(
        connection, device_filename)
    connection.Close()
    return mode, size, mtime

  def List(self, device_path):
    """Return a directory listing of the given path.

    Args:
      device_path: Directory to list.
    """
    connection = self.protocol_handler.Open(self.handle, destination=b'sync:')
    listing = self.filesync_handler.List(connection, device_path)
    connection.Close()
    return listing

  def Reboot(self, destination=b''):
    """Reboot the device.

    Args:
      destination: Specify 'bootloader' for fastboot.
    """
    self.protocol_handler.Open(self.handle, b'reboot:%s' % destination)

  def RebootBootloader(self):
    """Reboot device into fastboot."""
    self.Reboot(b'bootloader')

  def Remount(self):
    """Remount / as read-write."""
    return self.protocol_handler.Command(self.handle, service=b'remount')

  def Root(self):
    """Restart adbd as root on the device."""
    return self.protocol_handler.Command(self.handle, service=b'root')

  def EnableVerity(self):
    """Re-enable dm-verity checking on userdebug builds"""
    return self.protocol_handler.Command(self.handle, service=b'enable-verity')

  def DisableVerity(self):
    """Disable dm-verity checking on userdebug builds"""
    return self.protocol_handler.Command(self.handle, service=b'disable-verity')

  def Shell(self, command, timeout_ms=None):
    """Run command on the device, returning the output.

    Args:
      command: Shell command to run
      timeout_ms: Maximum time to allow the command to run.
    """
    return self.protocol_handler.Command(
        self.handle, service=b'shell', command=command,
        timeout_ms=timeout_ms)

  def StreamingShell(self, command, timeout_ms=None):
    """Run command on the device, yielding each line of output.

    Args:
      command: Command to run on the target.
      timeout_ms: Maximum time to allow the command to run.

    Yields:
      The responses from the shell command.
    """
    return self.protocol_handler.StreamingCommand(
        self.handle, service=b'shell', command=command,
        timeout_ms=timeout_ms)

  def Logcat(self, options, timeout_ms=None):
    """Run 'shell logcat' and stream the output to stdout.

    Args:
      options: Arguments to pass to 'logcat'.
      timeout_ms: Maximum time to allow the command to run.
    """
    return self.StreamingShell('logcat %s' % options, timeout_ms)

  def InteractiveShell(self, cmd=None, strip_cmd=True, delim=None, strip_delim=True):
    """Get stdout from the currently open interactive shell and optionally run a command
        on the device, returning all output.

    Args:
      command: Optional. Command to run on the target.
      strip_cmd: Optional (default True). Strip command name from stdout.
      delim: Optional. Delimiter to look for in the output to know when to stop expecting more output
      (usually the shell prompt)
      strip_delim: Optional (default True): Strip the provided delimiter from the output

    Returns:
      The stdout from the shell command.
    """
    conn = self._get_service_connection(b'shell:')

    return self.protocol_handler.InteractiveShellCommand(conn, cmd=cmd, strip_cmd=strip_cmd, delim=delim, strip_delim=strip_delim)