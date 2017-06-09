# Copyright 2017 Google Inc. All rights reserved.
# Use of this source code is governed by the Apache 2.0 license that can be
# found in the LICENSE file.
"""ADB command-line interface"""
import logging
import os
import platform
import re
import subprocess
from threading import Timer
import time
import monotonic

class Adb(object):
    """ADB command-line interface"""
    def __init__(self, options, cache_dir):
        self.options = options
        self.device = options.device
        self.rndis = options.rndis
        self.ping_address = None
        self.screenrecord = None
        self.tcpdump = None
        self.version = None
        self.kernel = None
        self.short_version = None
        self.last_bytes_rx = 0
        self.initialized = False
        self.this_path = os.path.abspath(os.path.dirname(__file__))
        self.root_path = os.path.abspath(os.path.join(self.this_path, os.pardir))
        self.cache_dir = cache_dir
        self.simplert_path = None
        self.simplert = None
        self.known_apps = {
            'com.motorola.ccc.ota': {},
            'com.google.android.apps.docs': {},
            'com.samsung.android.MtpApplication': {}
        }
        self.exe = 'adb'

    def run(self, cmd, timeout_sec=60, silent=False):
        """Run a shell command with a time limit and get the output"""
        if not silent:
            logging.debug(' '.join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self.wait_for_process(proc, timeout_sec, silent)

    def wait_for_process(self, proc, timeout_sec=10, silent=False):
        """Wait for the given process to exit gracefully and return the result"""
        stdout = None
        kill_proc = lambda p: p.kill()
        timer = Timer(timeout_sec, kill_proc, [proc])
        try:
            timer.start()
            stdout, _ = proc.communicate()
            if not silent and stdout is not None and len(stdout):
                logging.debug(stdout[:100])
        except Exception:
            logging.debug('Error waiting for process to exit')
        finally:
            if timer is not None:
                timer.cancel()
        return stdout


    def build_adb_command(self, args):
        """Build an adb command with the (optional) device ID"""
        cmd = [self.exe]
        if self.device is not None:
            cmd.extend(['-s', self.device])
        cmd.extend(args)
        return cmd

    def shell(self, args, timeout_sec=60, silent=False):
        """Run an adb shell command"""
        cmd = self.build_adb_command(['shell'])
        cmd.extend(args)
        return self.run(cmd, timeout_sec, silent)

    # pylint: disable=C0103
    def su(self, command, timeout_sec=60, silent=False):
        """Run a command as su"""
        cmd = ['su', '-c', command]
        return self.shell(cmd, timeout_sec, silent)
    # pylint: enable=C0103

    def adb(self, args, silent=False):
        """Run an arbitrary adb command"""
        cmd = self.build_adb_command(args)
        if not silent:
            logging.debug(' '.join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.wait_for_process(proc, 120, silent)
        return bool(proc.returncode is not None and proc.returncode == 0)

    def start(self):
        """ Do some startup check to make sure adb is installed"""
        import psutil
        ret = False
        out = self.run(self.build_adb_command(['devices']))
        if out is not None:
            ret = True
            # Set the CPU affinity for adb which helps avoid hangs
            for proc in psutil.process_iter():
                if proc.name() == "adb.exe" or proc.name() == "adb" or proc.name() == "adb-arm":
                    proc.cpu_affinity([0])
            # Start the simple-rt process if needed
            self.simplert_path = None
            if self.options.simplert is not None and platform.system() == 'Linux':
                from .os_util import process_running
                if not process_running('simple-rt'):
                    if os.uname()[4].startswith('arm'):
                        self.simplert_path = os.path.join(self.root_path, 'simple-rt', 'arm')
                    elif platform.architecture()[0] == '64bit':
                        self.simplert_path = os.path.join(self.root_path, 'simple-rt', 'linux64')
            if self.simplert_path is not None:
                self.shell(['am', 'force-stop', 'com.viper.simplert'])
                from .os_util import kill_all
                from .os_util import wait_for_all
                kill_all('simple-rt', False)
                wait_for_all('simple-rt')
                logging.debug('Starting simple-rt bridge process')
                interface, dns = self.options.simplert.split(',', 1)
                exe = os.path.join(self.simplert_path, 'simple-rt')
                command = ['sudo', exe, '-i', interface]
                if dns is not None and len(dns):
                    command.extend(['-n', dns])
                self.simplert = subprocess.Popen(' '.join(command), shell=True,
                                                 cwd=self.simplert_path)
        return ret

    def stop(self):
        """Shut down anything necessary"""
        if self.simplert is not None:
            self.shell(['am', 'force-stop', 'com.viper.simplert'])
            logging.debug('Stopping simple-rt bridge process')
            from .os_util import kill_all
            from .os_util import wait_for_all
            kill_all('simple-rt', False)
            wait_for_all('simple-rt')
            self.simplert = None

    def kill_proc(self, procname, kill_signal='-SIGINT'):
        """Kill all processes with the given name"""
        out = self.shell(['ps', '|', 'grep', procname])
        if out is not None:
            for line in out.splitlines():
                match = re.search(r'^\s*[^\s]+\s+(\d+)', line)
                if match:
                    pid = match.group(1)
                    self.shell(['kill', kill_signal, pid])

    def kill_proc_su(self, procname, kill_signal='-SIGINT'):
        """Kill all processes with the given name"""
        out = self.su('ps')
        if out is not None:
            for line in out.splitlines():
                if line.find(procname) >= 0:
                    match = re.search(r'^\s*[^\s]+\s+(\d+)', line)
                    if match:
                        pid = match.group(1)
                        self.su('kill {0} {1}'.format(kill_signal, pid))

    def start_screenrecord(self):
        """Start a screenrecord session on the device"""
        self.shell(['rm', '/data/local/tmp/wpt_video.mp4'])
        try:
            cmd = self.build_adb_command(['shell', 'screenrecord', '--verbose',
                                          '--bit-rate', '8000000',
                                          '/data/local/tmp/wpt_video.mp4'])
            self.screenrecord = subprocess.Popen(cmd)
        except Exception:
            pass

    def stop_screenrecord(self, local_file):
        """Stop a screen record and download the video to local_file"""
        if self.screenrecord is not None:
            logging.debug('Stopping screenrecord')
            self.kill_proc('screenrecord')
            self.wait_for_process(self.screenrecord)
            self.screenrecord = None
            self.adb(['pull', '/data/local/tmp/wpt_video.mp4', local_file])
            self.shell(['rm', '/data/local/tmp/wpt_video.mp4'])

    def start_tcpdump(self):
        """Start a tcpdump capture"""
        tcpdump_binary = '/data/local/tmp/tcpdump474'
        capture_file = '/data/local/tmp/tcpdump.cap'
        local_binary = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                                    'support', 'android', 'tcpdump')
        out = self.su('ls {0}'.format(tcpdump_binary))
        if out.find('No such') > -1:
            self.adb(['push', local_binary, tcpdump_binary])
            self.su('chown root {0}'.format(tcpdump_binary))
            self.su('chmod 755 {0}'.format(tcpdump_binary))
        cmd = self.build_adb_command(['shell', 'su', '-c',
                                      '{0} -i any -p -s 0 -w {1}'.format(tcpdump_binary,
                                                                         capture_file)])
        try:
            logging.debug(' '.join(cmd))
            self.tcpdump = subprocess.Popen(cmd)
        except Exception:
            pass

    def stop_tcpdump(self, local_file):
        """Stop a tcpdump capture and download to local_file"""
        if self.tcpdump is not None:
            logging.debug('Stopping tcpdump')
            capture_file = '/data/local/tmp/tcpdump.cap'
            self.kill_proc_su('tcpdump474')
            self.wait_for_process(self.tcpdump)
            self.tcpdump = None
            self.su('chmod 666 {0}'.format(capture_file))
            self.adb(['pull', capture_file, local_file])
            self.su('rm {0}'.format(capture_file))

    def get_battery_stats(self):
        """Get the temperature andlevel of the battery"""
        ret = {}
        out = self.shell(['dumpsys', 'battery'], silent=True)
        if out is not None:
            for line in out.splitlines():
                match = re.search(r'^\s*level:\s*(\d+)', line)
                if match:
                    ret['level'] = int(match.group(1))
                match = re.search(r'^\s*temperature:\s*(\d+)', line)
                if match:
                    ret['temp'] = float(match.group(1)) / 10.0
        return ret

    def ping(self, address):
        """Ping the provided network address"""
        ret = None
        if address is not None:
            out = self.shell(['ping', '-n', '-c3', '-i0.2', '-w5', address], silent=True)
            if out is not None:
                for line in out.splitlines():
                    match = re.search(r'^\s*rtt\s[^=]*=\s*([\d\.]*)', line)
                    if match:
                        ret = float(match.group(1))
            if ret is None:
                logging.debug('%s is unreachable', address)
            else:
                logging.debug('%s rtt %0.3f ms', address, ret)
        return ret

    def cleanup_device(self):
        """Do some device-level cleanup"""
        # Clear notifications
        self.su('service call notification 1')
        # Close some known apps that pop-over
        for app in self.known_apps:
            if 'installed' not in self.known_apps[app]:
                out = self.shell(['dumpsys', 'package', app, '|', 'grep', 'versionName', '|',
                                  'head', '-n1'])
                self.known_apps[app]['installed'] = bool(out is not None and len(out.strip()))
            if self.known_apps[app]['installed']:
                self.shell(['am', 'force-stop', app])
        # Cleanup the downloads folders
        self.shell(['rm', '-rf', '/sdcard/Download/*', '/sdcard/Backucup', '/sdcard/UCDownloads',
                    '/data/local/tmp/tcpdump.cap', '/data/local/tmp/wpt_video.mp4'])
        self.su('rm -rf /data/media/0/Download/* /data/media/0/Backucup /data/media/0/UCDownloads')
        # See if there are any system dialogs that need dismissing
        out = self.shell(['dumpsys', 'window', 'windows'], silent=True)
        if re.search(r'Window #[^\n]*Application Error\:', out) is not None or \
                re.search(r'Window #[^\n]*systemui\.usb\.UsbDebuggingActivity', out) is not None:
            logging.warning('Dismissing system dialog')
            self.shell(['input', 'keyevent', 'KEYCODE_DPAD_RIGHT'], silent=True)
            self.shell(['input', 'keyevent', 'KEYCODE_DPAD_RIGHT'], silent=True)
            self.shell(['input', 'keyevent', 'KEYCODE_ENTER'], silent=True)

    def get_rndis_interface(self):
        """Return the name of the rndis interface, it's state and assigned address"""
        interface = None
        if_state = None
        address = None
        out = self.shell(['ip', 'address', 'show'], silent=True)
        need_address = False
        if out is not None:
            for line in out.splitlines():
                match = re.search(r'[\d]+\:\s+([^:]+):[^\n]*state (\w+)', line)
                if match:
                    need_address = False
                    iface = match.group(1)
                    if iface == 'rndis0':
                        interface = iface
                        if_state = match.group(2)
                        if_state = if_state.lower()
                        address = None
                        need_address = True
                    elif interface is None and iface == 'usb0':
                        interface = iface
                        if_state = match.group(2)
                        if_state = if_state.lower()
                        address = None
                        need_address = True
                elif need_address:
                    match = re.search(r'^\s*inet ([\d\.]+)', line)
                    if match:
                        address = match.group(1)
        return interface, if_state, address

    def check_rndis(self):
        """Bring up the rndis interface if it isn't up"""
        rndis_ready = False
        is_dhcp = bool(self.rndis == 'dhcp')
        rndis_address = None
        if not is_dhcp:
            match = re.search(r'^([\d\.]+\/\d+),([\d\.]+),([\d\.]+),([\d\.]+)', self.rndis)
            if match:
                rndis_address = {'addr': match.group(1),
                                 'gateway': match.group(2),
                                 'dns1': match.group(3),
                                 'dns2': match.group(4)}
            else:
                logging.error('Invalid rndis address config: %s', self.rndis)
        interface, if_state, address = self.get_rndis_interface()
        if interface is not None and if_state == 'up' and address is not None:
            rndis_ready = True
        elif is_dhcp or rndis_address is not None:
            # Make sure the USB interface is configured for rndis
            out = self.shell(['getprop', 'sys.usb.config'], silent=True)
            if out.strip() != 'rndis,adb':
                logging.debug('Enabling rndis USB mode')
                self.su('setprop sys.usb.config rndis,adb')
                self.adb(['wait-for-device'])
            # Enable tethering (function is different depending on Android version)
            tether_function = '34'
            if self.short_version >= 6.0:
                tether_function = '41' if self.kernel == 'android-samsung' else '30'
            elif self.short_version >= 5.1:
                tether_function = '31'
            elif self.short_version >= 5.0:
                tether_function = '30'
            elif self.short_version >= 4.4:
                tether_function = '34'
            elif self.short_version >= 4.1:
                tether_function = '33'
            elif self.short_version >= 4.0:
                tether_function = '32'
            self.su('service call connectivity {0} i32 1'.format(tether_function))
            self.adb(['wait-for-device'])
            interface, if_state, address = self.get_rndis_interface()
            if interface is not None:
                self.su('svc wifi disable')
                # turn down all of the other interfaces
                out = self.su('ip link show')
                if out is not None:
                    for line in out:
                        match = re.search(r'[\d]+\:\s+([^:]+):[^\n]*state (\w+)', line)
                        if match:
                            iface = match.group(1)
                            if iface != interface and iface != 'lo' and iface[:4] != 'wlan':
                                self.su('ip link set {0} down'.format(iface))
                if rndis_address is not None:
                    # Set up the address
                    self.su('ip rule add from all lookup main')
                    self.su('ip link set {0} down'.format(interface))
                    self.su('ip addr flush dev {0}'.format(interface))
                    self.su('ip addr add {0} dev {1}'.format(rndis_address['addr'], interface))
                    self.su('ip link set {0} up'.format(interface))
                    # Set up the gateway
                    self.su('route add -net 0.0.0.0 netmask 0.0.0.0 gw {0} dev {1}'.format(
                        rndis_address['gateway'], interface))
                    self.su('setprop net.{0}.gw {1}'.format(interface, rndis_address['gateway']))
                    self.su('setprop net.{0}.gateway {1}'.format(interface,
                                                                 rndis_address['gateway']))
                    # Configure DNS
                    self.su('setprop net.dns1 {0}'.format(rndis_address['dns1']))
                    self.su('setprop net.dns2 {0}'.format(rndis_address['dns2']))
                    self.su('setprop net.{0}.dns1 {1}'.format(interface, rndis_address['dns1']))
                    self.su('setprop net.{0}.dns2 {1}'.format(interface, rndis_address['dns2']))
                    self.su('ndc resolver setifdns {0} {1} {2}'.format(interface,
                                                                       rndis_address['dns1'],
                                                                       rndis_address['dns2']))
                    self.su('ndc resolver setdefaultif {0}'.format(interface))
                    # Misc settings
                    self.su('setprop "net.gprs.http-proxy" ""')
                    interface, if_state, address = self.get_rndis_interface()
                    if interface is not None and if_state == 'up' and address is not None:
                        rndis_ready = True
                elif is_dhcp:
                    self.su('netcfg {0} dhcp'.format(interface))

        return rndis_ready

    def is_tun_interface_available(self):
        """Check to see if tun0 is up"""
        is_ready = False
        out = self.shell(['ip', 'address', 'show'], silent=True)
        if out is not None:
            for line in out.splitlines():
                if re.search(r'^[\d]+\:\s+tun0:', line):
                    is_ready = True
        return is_ready

    def dismiss_vpn_dialog(self):
        """Check and see if the VPN permission dialog is up and dismiss it"""
        out = self.shell(['dumpsys', 'window', 'windows'], silent=True)
        if out.find('com.android.vpndialogs') >= 0:
            logging.warning('Dismissing VPN dialog')
            self.shell(['input', 'keyevent', 'KEYCODE_DPAD_RIGHT'], silent=True)
            self.shell(['input', 'keyevent', 'KEYCODE_ENTER'], silent=True)
            self.shell(['input', 'keyevent', 'KEYCODE_DPAD_RIGHT'], silent=True)
            self.shell(['input', 'keyevent', 'KEYCODE_ENTER'], silent=True)

    def reset_simplert(self):
        """Reset the tunnel on the phone in case it's state is messed up"""
        self.shell(['am', 'force-stop', 'com.viper.simplert'])

    def check_simplert(self):
        """Bring up the simple-rt bridge if it isn't running"""
        is_ready = self.is_tun_interface_available()
        if not is_ready:
            # disconnect/reconnect the USB interface
            self.su('setprop sys.usb.config adb')
            self.adb(['wait-for-device'])
            # wait up to 30 seconds for the interface to come up
            end_time = monotonic.monotonic() + 30
            while not is_ready and monotonic.monotonic() < end_time:
                time.sleep(1)
                self.dismiss_vpn_dialog()
                is_ready = self.is_tun_interface_available()
        if not is_ready:
            logging.debug('simplert bridge not started')
        return is_ready

    def is_device_ready(self):
        """Check to see if the device is ready to run tests"""
        is_ready = True
        if self.version is None:
            # Turn down the volume (just one notch each time it is run)
            self.shell(['input', 'keyevent', '25'])
            self.cleanup_device()
            out = self.shell(['getprop', 'ro.build.version.release'], silent=True)
            if out is not None:
                self.version = 'Android ' + out.strip()
                match = re.search(r'^(\d+\.\d+)', out)
                if match:
                    self.short_version = float(match.group(1))
        if self.kernel is None:
            out = self.shell(['getprop', 'ro.com.google.clientidbase'], silent=True)
            if out is not None:
                self.kernel = out.strip()
        battery = self.get_battery_stats()
        logging.debug(battery)
        if 'level' in battery and battery['level'] < 50:
            logging.info("Device not ready, low battery: %d %%", battery['level'])
            is_ready = False
        if 'temp' in battery and battery['temp'] > 36.0:
            logging.info("Device not ready, high temperature: %0.1f degrees", battery['temp'])
            is_ready = False
        # Bring up the bridged interface if necessary
        if self.rndis is not None:
            is_ready = self.check_rndis()
        if self.options.simplert is not None:
            is_ready = self.check_simplert()
            if not is_ready:
                self.reset_simplert()
        # Try pinging the network (prefer the gateway but fall back to DNS or 8.8.8.8)
        if is_ready:
            net_ok = False
            if self.ping(self.ping_address) is not None:
                net_ok = True
            else:
                addresses = []
                props = self.shell(['getprop'])
                gateway = None
                if props is not None:
                    for line in props.splitlines():
                        match = re.search(r'^\[net\.dns\d\]:\s+\[([^\]]*)\]', line)
                        if match:
                            dns = match.group(1)
                            if dns not in addresses:
                                addresses.append(dns)
                        match = re.search(r'^\[dhcp\.[^\.]+\.dns\d\]:\s+\[([^\]]*)\]', line)
                        if match:
                            dns = match.group(1)
                            if dns not in addresses:
                                addresses.append(dns)
                        match = re.search(r'^\[dhcp\.[^\.]+\.gateway\]:\s+\[([^\]]*)\]', line)
                        if match:
                            gateway = match.group(1)
                if gateway is not None:
                    addresses.insert(0, gateway)
                addresses.append('8.8.8.8')
                for address in addresses:
                    if self.ping(address) is not None:
                        self.ping_address = address
                        net_ok = True
                        break
            if not net_ok:
                logging.info("Device not ready, network not responding")
                if self.options.simplert is not None:
                    self.reset_simplert()
                is_ready = False
        if is_ready and not self.initialized:
            self.initialized = True
            # Disable emergency alert notifications
            self.su('pm disable com.android.cellbroadcastreceiver')
        return is_ready

    def get_jiffies_time(self):
        """Get the uptime in nanoseconds and jiffies for hz calculation"""
        out = self.shell(['cat', '/proc/timer_list'], silent=True)
        nsecs = None
        jiffies = None
        if out is not None:
            for line in out.splitlines():
                if nsecs is None:
                    match = re.search(r'^now at (\d+) nsecs', line)
                    if match:
                        nsecs = int(match.group(1))
                if jiffies is None:
                    match = re.search(r'^jiffies:\s+(\d+)', line)
                    if match:
                        jiffies = int(match.group(1))
        return nsecs, jiffies

    def get_bytes_rx(self):
        """Get the incremental bytes received across all non-loopback interfaces"""
        bytes_rx = 0
        out = self.shell(['cat', '/proc/net/dev'], silent=True)
        if out is not None:
            for line in out.splitlines():
                match = re.search(r'^\s*(\w+):\s+(\d+)', line)
                if match:
                    interface = match.group(1)
                    if interface != 'lo':
                        bytes_rx += int(match.group(2))
        delta = bytes_rx - self.last_bytes_rx
        self.last_bytes_rx = bytes_rx
        return delta

    def get_video_size(self):
        """Get the current size of the video file"""
        size = 0
        out = self.shell(['ls', '-l', '/data/local/tmp/wpt_video.mp4'], silent=True)
        match = re.search(r'[^\d]+\s+(\d+) \d+', out)
        if match:
            size = int(match.group(1))
        return size

    def screenshot(self, dest_file):
        """Capture a png screenshot of the device"""
        device_path = '/data/local/tmp/wpt_screenshot.png'
        self.shell(['rm', '/data/local/tmp/wpt_screenshot.png'], silent=True)
        self.shell(['screencap', '-p', device_path])
        self.adb(['pull', device_path, dest_file])

    def get_package_version(self, package):
        """Get the version number of the given package"""
        version = None
        out = self.shell(['dumpsys', 'package', package, '|', 'grep', 'versionName'])
        if out is not None:
            for line in out.splitlines():
                separator = line.find('=')
                if separator > -1:
                    ver = line[separator + 1:].strip()
                    if len(ver):
                        version = ver
                        logging.debug('Package version for %s is %s', package, version)
                        break
        return version
