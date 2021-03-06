#
# Copyright 2010-2012 Red Hat, Inc. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Refer to the README and COPYING files for full details of the license.
#

import logging
import os
import socket
import subprocess
import threading
import time
from OVirtAgentLogic import AgentLogicBase, DataRetriverBase

CredServer = None
try:
    from CredServer import CredServer as CredServerImported
    CredServer = CredServerImported
except ImportError:
    # The CredServer doesn't exist in RHEL-5. So we provide a
    # fake server that do nothing.
    class CredServerFake(threading.Thread):
        def user_authenticated(self, credentials):
            pass
    CredServer = CredServerFake


class PkgMgr(object):

    def rpm_list_packages(self, app_list):
        """ Implementes the package retrieval for rpm based environments """
        apps = set()
        for name in app_list.split():
            ts = self.rpm.TransactionSet()
            for app in ts.dbMatch('name', name):
                apps.add("%s-%s-%s" %
                         (app['name'], app['version'], app['release']))
        return apps

    def apt_list_packages(self, app_list):
        """ Implementes the package retrieval for apt based environments """
        INSTALLED_STATE = self.apt_pkg.CURSTATE_INSTALLED
        apps = set()
        cache = self.apt_pkg.Cache()
        for app in app_list.split():
            if app in cache:
                pkg = cache[app]
                # Normal package
                if pkg.current_state == INSTALLED_STATE:
                    detail = (app, pkg.current_ver.ver_str)
                    apps.add("%s-%s" % (detail))
                # virtual package
                elif len(pkg.provides_list) > 0:
                    for _, _, pkg in pkg.provides_list:
                        if pkg.parent_pkg.current_state == INSTALLED_STATE:
                            detail = (app, pkg.parent_pkg.current_ver.ver_str)
                            apps.add("%s-%s" % (detail))
        return apps

    def list_pkgs(self, app_list):
        """ Implements the package retrieval for apt and rpm if present and
            returns a joined list of packages installed on the system. """
        apps = set()
        if self.rpm:
            apps.update(self.rpm_list_packages(app_list))
        if self.apt_pkg:
            apps.update(self.apt_list_packages(app_list))
        apps = list(apps)
        logging.debug("PkgMgr: list_pkgs returns [%s]" % (str(apps)))
        return apps

    def __init__(self):
        self.rpm = None
        self.apt_pkg = None
        try:
            import rpm
            self.rpm = rpm
        except ImportError:
            pass

        try:
            from apt import apt_pkg
            apt_pkg.init()
            self.apt_pkg = apt_pkg
        except ImportError:
            pass

        if not self.rpm and not self.apt_pkg:
            logging.info("Unknown package management. Application list "
                         "report is disabled.")


class NicMgr(object):

    def __init__(self):
        try:
            import ethtool
        except ImportError:
            raise NotImplementedError
        self.ethtool = ethtool
        self.list_nics = self.ethtool_list_nics

    def _get_ipv4_addresses(self, dev):
        if hasattr(dev, 'get_ipv4_addresses'):
            ipv4_addrs = []
            for ip in dev.get_ipv4_addresses():
                ipv4_addrs.append(ip.address)
            return ipv4_addrs
        if dev.ipv4_address is not None:
            return [dev.ipv4_address]
        else:
            return []

    def _get_ipv6_addresses(self, dev):
        ipv6_addrs = []
        for ip in dev.get_ipv6_addresses():
            ipv6_addrs.append(ip.address)
        return ipv6_addrs

    def ethtool_list_nics(self):
        interfaces = list()
        try:
            for dev in self.ethtool.get_devices():
                flags = self.ethtool.get_flags(dev)
                if flags & self.ethtool.IFF_UP and \
                        not(flags & self.ethtool.IFF_LOOPBACK):
                    devinfo = self.ethtool.get_interfaces_info(dev)[0]
                    interfaces.append(
                        {'name': dev,
                         'inet': self._get_ipv4_addresses(devinfo),
                         'inet6': self._get_ipv6_addresses(devinfo),
                         'hw': self.ethtool.get_hwaddr(dev)})
        except:
            logging.exception("Error retrieving network interfaces.")
        interfaces.sort(lambda x,y : cmp(x['name'], y['name'])) 
        return interfaces


class CommandHandlerLinux:

    def __init__(self, agent):
        self.agent = agent

    def lock_screen(self):
        cmd = ['/usr/share/ovirt-guest-agent/ovirt-locksession']
        logging.debug("Executing lock session command: '%s'", cmd)
        subprocess.call(cmd)

    def login(self, credentials):
        self.agent.cred_server.user_authenticated(credentials)

    def logoff(self):
        pass

    def shutdown(self, timeout, msg, reboot=False):
        # The shutdown command works with minutes while vdsm send value in
        # seconds, so we round up the value to minutes.
        delay = (int(timeout) + 59) / 60
        param = '-h'
        action = 'shutdown'
        if reboot:
            param = '-r'
            action = 'reboot'
        cmd = ['/usr/share/ovirt-guest-agent/ovirt-shutdown', param,
               "+%d" % (delay), "\"%s\"" % (msg)]

        logging.debug("Executing %s command: %s", action, cmd)
        subprocess.call(cmd)

    def hibernate(self, state):
        cmd = ['/usr/share/ovirt-guest-agent/ovirt-hibernate', state]
        logging.debug("Executing hibernate command: %s", cmd)
        subprocess.call(cmd)

    def set_number_of_cpus(self, count):
        pass  # currently noop


class LinuxDataRetriver(DataRetriverBase):

    def __init__(self):
        try:
            pkgmgr = PkgMgr()
        except NotImplementedError:
            self.list_pkgs = lambda app_list: []
        else:
            self.list_pkgs = pkgmgr.list_pkgs
        try:
            nicmgr = NicMgr()
        except NotImplementedError:
            self.list_nics = lambda: []
        else:
            self.list_nics = nicmgr.list_nics
        self.app_list = ""
        self.ignored_fs = ""
        self._init_vmstat()
        DataRetriverBase.__init__(self)

    def getMachineName(self):
        return socket.getfqdn()

    def getOsVersion(self):
        return os.uname()[2]

    def getAllNetworkInterfaces(self):
        return self.list_nics()

    def getApplications(self):
        return self.list_pkgs(self.app_list)

    def getAvailableRAM(self):
        free = 0
        for line in open('/proc/meminfo'):
            var, value = line.strip().split()[0:2]
            if var in ('MemFree:', 'Buffers:', 'Cached:'):
                free += long(value)
        return str(free / 1024)

    def getUsers(self):
        users = ''
        try:
            cmdline = '/usr/bin/users | /usr/bin/tr " " "\n" | /usr/bin/uniq'
            users = ' '.join(os.popen(cmdline).read().split())
        except:
            logging.exception("Error retrieving logged in users.")
        return users

    def getActiveUser(self):
        users = os.popen('/usr/bin/users').read().split()
        try:
            user = users[0]
        except:
            user = 'None'
        return user

    def getDisksUsage(self):
        usages = list()
        try:
            mounts = open('/proc/mounts', 'r')
            for mount in mounts:
                try:
                    (device, path, fs) = mount.split()[:3]
                    if fs not in self.ignored_fs:
                        # path might include spaces.
                        path = path.decode("string-escape")
                        statvfs = os.statvfs(path)
                        total = statvfs.f_bsize * statvfs.f_blocks
                        used = total - statvfs.f_bsize * statvfs.f_bfree
                        usages.append({'path': path, 'fs': fs, 'total': total,
                                       'used': used})
                except:
                    logging.exception("Error retrieving disks usages.")
            mounts.close()
        except Exception:
            logging.exception("Error during reading mounted devices")
            if mounts:
                mounts.close()

        return usages

    def getMemoryStats(self):
        try:
            self._get_meminfo()
            self._get_vmstat()
        except:
            logging.exception("Error retrieving memory stats.")
        return self.memStats

    def _init_vmstat(self):
        self.vmstat = {}
        self.vmstat['timestamp_prev'] = time.time()
        fields = ['swap_in', 'swap_out', 'pageflt', 'majflt']
        for field in fields:
            self.vmstat[field + '_prev'] = None
            self.vmstat[field + '_cur'] = None

    def _get_meminfo(self):
        fields = {'MemTotal:': 0, 'MemFree:': 0, 'Buffers:': 0,
                  'Cached:': 0, 'SwapFree:': 0, 'SwapTotal:': 0}
        free = 0
        for line in open('/proc/meminfo'):
            (key, value) = line.strip().split()[0:2]
            if key in fields.keys():
                fields[key] = int(value)
            if key in ('MemFree:', 'Buffers:', 'Cached:'):
                free += int(value)

        self.memStats['mem_total'] = fields['MemTotal:']
        self.memStats['mem_unused'] = fields['MemFree:']
        self.memStats['mem_free'] = free
        self.memStats['mem_buffers'] = fields['Buffers:']
        self.memStats['mem_cached'] = fields['Cached:']
        swap_used = fields['SwapTotal:'] - fields['SwapFree:']
        self.memStats['swap_usage'] = swap_used
        self.memStats['swap_total'] = fields['SwapTotal:']

    def _get_vmstat(self):
        """
        /proc/vmstat reports cumulative statistics so we must subtract the
        previous values to get the difference since the last collection.
        """
        fields = {'pswpin': 'swap_in', 'pswpout': 'swap_out',
                  'pgfault': 'pageflt', 'pgmajfault': 'majflt'}

        self.vmstat['timestamp_cur'] = time.time()
        interval = self.vmstat['timestamp_cur'] - self.vmstat['timestamp_prev']
        self.vmstat['timestamp_prev'] = self.vmstat['timestamp_cur']

        for line in open('/proc/vmstat'):
            (key, value) = line.strip().split()[0:2]
            if key in fields.keys():
                name = fields[key]
                self.vmstat[name + '_prev'] = self.vmstat[name + '_cur']
                self.vmstat[name + '_cur'] = int(value)
                if self.vmstat[name + '_prev'] is None:
                    self.vmstat[name + '_prev'] = self.vmstat[name + '_cur']
                self.memStats[name] = int((self.vmstat[name + '_cur'] -
                                           self.vmstat[name + '_prev']) /
                                          interval)


class LinuxVdsAgent(AgentLogicBase):

    def __init__(self, config):
        AgentLogicBase.__init__(self, config)
        self.dr = LinuxDataRetriver()
        self.dr.app_list = config.get("general", "applications_list")
        self.dr.ignored_fs = set(config.get("general", "ignored_fs").split())
        self.commandHandler = CommandHandlerLinux(self)
        self.cred_server = CredServer()

    def run(self):
        self.cred_server.start()
        AgentLogicBase.run(self)

    def stop(self):
        self.cred_server.join()
        AgentLogicBase.stop(self)


def test():
    from pprint import pprint
    dr = LinuxDataRetriver()
    dr.app_list = "kernel kernel-headers aspell"
    dr.ignored_fs = set("rootfs tmpfs autofs cgroup selinuxfs udev mqueue "
                        "nfsd proc sysfs devtmpfs hugetlbfs rpc_pipefs devpts "
                        "securityfs debugfs binfmt_misc fuse.gvfsd-fuse "
                        "fuse.gvfs-fuse-daemon fusectl usbfs".split())
    print "Machine Name:", dr.getMachineName()
    print "Fully Qualified Domain Name:", dr.getFQDN()
    print "OS Version:", dr.getOsVersion()
    print "Network Interfaces:",
    pprint(dr.getAllNetworkInterfaces())
    print "Installed Applications:",
    pprint(dr.getApplications())
    print "Available RAM:", dr.getAvailableRAM()
    print "Logged in Users:", dr.getUsers()
    print "Active User:", dr.getActiveUser()
    print "Disks Usage:",
    pprint(dr.getDisksUsage())
    print "Memory Stats:", dr.getMemoryStats()

if __name__ == '__main__':
    test()
