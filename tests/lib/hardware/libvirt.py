# Copyright (c) 2020 SUSE LINUX GmbH
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

# The Hardware module should take care of the operating system abstraction
# through images.
# libcloud will provide a common set of cloud-agnostic objects such as Node[s]
# We might extend the Node object to have an easy way to run arbitrary commands
# on the node such as Node.execute().
# There will be a challenge where those arbitrary commands differ between OS's;
# this is an abstraction that is not yet well figured out, but will likely
# take the form of cloud-init or similar bringing the target node to an
# expected state.

import os
import shutil
import subprocess
import time
import tempfile
import threading
import datetime
import logging
import libvirt
import uuid
import paramiko
import socket
from typing import List
from xml.dom import minidom

from tests.lib.hardware.hardware_base import HardwareBase
from tests.lib.hardware.node_base import NodeBase, NodeRole
from tests import config

logger = logging.getLogger(__name__)


class Node(NodeBase):
    def __init__(self, name, role, tags, conn, network, disk_number, memory,
                 ssh_public_key, ssh_private_key):
        super().__init__(name, role, tags)
        self._conn = conn
        self._network = network
        self._disk_number = disk_number,
        self._memory = memory * 1024 * 1024
        self._ssh_public_key = ssh_public_key
        self._ssh_private_key = ssh_private_key
        self._snap_img_path = os.path.join(
            os.path.dirname(config.PROVIDER_LIBVIRT_IMAGE),
            f"{self.name}-snapshot.qcow2")
        self._cloud_init_seed_path = os.path.join(os.path.dirname(
            config.PROVIDER_LIBVIRT_IMAGE), f"{self.name}-cloud-init-seed.img")

    def boot(self):
        self._backing_file_create()
        self._cloud_init_seed_create()
        xml = self._get_domain(self.name, self._snap_img_path,
                               self._cloud_init_seed_path,
                               self._network.name(), self._memory)
        logger.info(f"node {self.name}: booting with "
                    f"image {self._snap_img_path}")
        logger.debug(f"node {self.name}: libvirt xml: {xml}")
        self._dom = self._conn.defineXML(xml)
        self._dom.create()
        self._ips = self._get_ips()
        self._wait_for_ssh()

    def destroy(self):
        self._dom.destroy()
        self._dom.undefine()

    def get_ssh_ip(self):
        return self._ips[0]

    def _wait_for_ssh(self, timeout=60):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ip = self.get_ssh_ip()
        stop = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
        logger.info(f"node {self.name}: waiting for ssh for {timeout} s")
        while datetime.datetime.now() < stop:
            try:
                ssh.connect(ip, username=config.NODE_IMAGE_USER,
                            key_filename=self._ssh_private_key)
                logger.info(f"node {self.name}: ssh ready for user"
                            f"{config.NODE_IMAGE_USER}")
                return
            except (paramiko.BadHostKeyException,
                    paramiko.AuthenticationException,
                    paramiko.ssh_exception.SSHException,
                    socket.error):
                time.sleep(3)
        raise Exception(f"node {self.name}: Timeout while waiting for "
                        f"ssh on {ip}")

    def _get_ips(self, timeout=45):
        """get the ip addresses of the guest domain from the DHCP leases"""
        ips_found = []
        xmldoc = minidom.parseString(self._dom.XMLDesc())
        mac_list = xmldoc.getElementsByTagName('mac')
        stop = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
        logger.info(f"node {self.name}: wait {timeout}s to get IP address")
        while len(mac_list) and datetime.datetime.now() < stop:
            for mac in mac_list:
                mac_addr = mac.attributes["address"].value
                for d in self._network.DHCPLeases():
                    if d['mac'].lower() == mac_addr.lower():
                        ips_found.append(d['ipaddr'])
            if len(ips_found):
                logger.info(f"node {self.name}: found IPs {ips_found}")
                return ips_found
            time.sleep(3)
        raise Exception(f"node {self.name}: no IP address found")

    def _backing_file_create(self):
        # TODO(toabctl): move the temp image to a different tmp dir
        if os.path.exists(self._snap_img_path):
            logger.info(f"node {self.name}: Delete available backing image "
                        f"{self._snap_img_path}")
            os.remove(self._snap_img_path)
        subprocess.check_call(f"qemu-img create -f qcow2 -F qcow2 -o "
                              f"backing_file={config.PROVIDER_LIBVIRT_IMAGE} "
                              f"{self._snap_img_path} 10G",
                              shell=True)
        logger.info(f"node {self.name}: created qcow2 backing file under"
                    f"{self._snap_img_path}")

    def _cloud_init_seed_create(self):
        user_data = """#cloud-config
debug: True
ssh_authorized_keys:
- {}
        """
        meta_data = """---
instance-id: {}
local-hostname: {}
        """

        iso_cmd = shutil.which('mkisofs')
        if not iso_cmd:
            raise Exception('mkisofs command not found')

        if os.path.exists(self._cloud_init_seed_path):
            os.remove(self._cloud_init_seed_path)
        with tempfile.TemporaryDirectory() as tempdir:
            with open(os.path.join(tempdir, 'user-data'), 'w') as ud:
                ud.write(user_data.format(self._ssh_public_key))
            with open(os.path.join(tempdir, 'meta-data'), 'w') as md:
                md.write(meta_data.format(uuid.uuid4(), self.name))
            # create the seed file
            args = [iso_cmd,
                    '-output', self._cloud_init_seed_path,
                    '-volid', 'cidata',
                    '-joliet', '-rock',
                    tempdir]
            subprocess.check_call(args, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)

    def _get_domain(self, domain_name, image, cloud_init_seed, network_name,
                    memory):
        return """
<domain type='kvm'>
<name>%(domain_name)s</name>
<memory unit='KiB'>%(memory)s</memory>
<currentMemory unit='KiB'>%(memory)s</currentMemory>
<vcpu placement='static'>2</vcpu>
<cpu mode='host-passthrough'>
</cpu>
<!--<cpu mode='host-model'>
<feature policy='require' name='vmx'/>
</cpu>-->
<os>
<type arch='x86_64' machine='pc-i440fx-2.1'>hvm</type>
<boot dev='hd'/>
</os>
<on_poweroff>destroy</on_poweroff>
<on_reboot>restart</on_reboot>
<on_crash>restart</on_crash>
<devices>
<emulator>/usr/bin/qemu-system-x86_64</emulator>
<disk type='file' device='disk'>
<driver name='qemu' type='qcow2' cache='none'/>
<source file='%(image)s'/>
<target dev='vda' bus='virtio'/>
</disk>
<disk type='file' device='cdrom'>
<driver name='qemu' type='raw' />
<source file='%(cloud_init_seed)s'/>
<target dev='sda' bus='sata'/>
<readonly/>
</disk>
<controller type='virtio-serial' index='0'>
<address type='pci' domain='0x0000' bus='0x00' slot='0x05' function='0x0'/>
</controller>
<interface type='network'>
<source network='%(network_name)s'/>
<model type='virtio'/>
<address type='pci' domain='0x0000' bus='0x00' slot='0x03' function='0x0'/>
</interface>
<serial type='pty'>
<target port='0'/>
</serial>
<console type='pty'>
<target type='serial' port='0'/>
</console>
<channel type='spicevmc'>
<target type='virtio' name='com.redhat.spice.0'/>
<address type='virtio-serial' controller='0' bus='0' port='1'/>
</channel>
<input type='mouse' bus='ps2'/>
<input type='keyboard' bus='ps2'/>
<graphics type='spice' autoport='yes'/>
<video>
<model type='vga'/>
</video>
<redirdev bus='usb' type='spicevmc'>
</redirdev>
<memballoon model='virtio'>
<address type='pci' domain='0x0000' bus='0x00' slot='0x06' function='0x0'/>
</memballoon>
</devices>
</domain>
        """ % {
            "domain_name": domain_name, "image": image,
            "cloud_init_seed": cloud_init_seed,
            "network_name": network_name,
            "memory": memory
        }


class Hardware(HardwareBase):
    def __init__(self):
        super().__init__()
        self._network = self.conn.networkLookupByName(
            config.PROVIDER_LIBVIRT_NETWORK)
        if not self._network:
            raise Exception(f'Can not get libvirt network '
                            '{config.PROVIDER_LIBVIRT_NETWORK}')
        logger.info(f"Got libvirt network {self._network.name()}")

    def get_connection(self):
        conn = libvirt.open(config.PROVIDER_LIBVIRT_CONNECTION)
        if not conn:
            raise Exception(f'Can not open libvirt connection '
                            '{config.PROVIDER_LIBVIRT_CONNECTION}')
        logger.debug(f"Got connection to libvirt: {conn}")
        return conn

    def _boot_node(self, name: str, role: NodeRole, tags: List[str]):
        # get a fresh connection to avoid threading problems
        conn = self.get_connection()
        node = Node(name, role, tags, conn, self._network, 0,
                    config.PROVIDER_LIBVIRT_VM_MEMORY,
                    self.public_key, self.private_key)
        node.boot()
        self.node_add(node)

    def boot_nodes(self, masters: int = 1, workers: int = 2, offset: int = 0):
        super().boot_nodes(masters, workers, offset)
        threads = []
        for c in range(0, masters):
            if c == 0:
                tags = ['master', 'first_master']
            else:
                tags = ['master']
            t = threading.Thread(
                target=self._boot_node, args=(
                    f"{self.hardware_uuid}-master-{c}",
                    NodeRole.MASTER, tags))
            threads.append(t)
            t.start()

        for c in range(0, workers):
            t = threading.Thread(
                target=self._boot_node, args=(
                    f"{self.hardware_uuid}-worker-{c}",
                    NodeRole.WORKER, ['worker']))
            threads.append(t)
            t.start()

        # wait for all threads to finish
        for t in threads:
            t.join()