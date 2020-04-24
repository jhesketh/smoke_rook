# Copyright (c) 2020 SUSE LINUX GmbH
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

# The Hardware module should take care of the operating system abstraction
# through images.
# libcloud will provide a common set of cloud-agnostic objects such as Node[s]
# We might extend the Node object to have an easy way to run arbitrary commands
# on the node such as Node.execute().
# There will be a challenge where those arbitrary commands differ between OS's;
# this is an abstraction that is not yet well figured out, but will likely
# take the form of cloud-init or similar bringing the target node to an
# expected state.

from tests.lib.distro import base


class SUSE(base.Distro):
    def wait_for_connection_play(self):
        # In order to be able to use mitogen we need to install python on the
        # nodes
        tasks = []

        tasks.append(
            dict(
                name="Wait for connection to hosts",
                action=dict(
                    module='wait_for_connection',
                    args=dict(
                        timeout=300
                    )
                )
            )
        )

        play_source = dict(
            name="Wait for nodes",
            hosts="all",
            tasks=tasks,
            gather_facts="no",
            strategy="free",
        )

        return play_source

    def bootstrap_play(self):
        tasks = []

        tasks.append(
            dict(
                name="Installing dependencies",
                action=dict(
                    module='zypper',
                    args=dict(
                        name=['bash-completion',
                              'ca-certificates',
                              'conntrack-tools',
                              'curl',
                              'docker',
                              'ebtables',
                              'ethtool',
                              'lvm2',
                              'lsof',
                              'ntp',
                              'socat',
                              'tree',
                              'vim',
                              'wget',
                              'xfsprogs'],
                        state='present',
                        extra_args_precommand='--non-interactive '
                                              '--gpg-auto-import-keys',
                        update_cache='yes',
                    )
                )
            )
        )

        tasks.append(
            dict(
                name="Updating kernel",
                action=dict(
                    module='zypper',
                    args=dict(
                        name='kernel-default',
                        state='latest',
                        extra_args_precommand='--non-interactive '
                                              '--gpg-auto-import-keys',
                    )
                )
            )
        )

        tasks.append(
            dict(
                name="Removing anti-dependencies",
                action=dict(
                    module='zypper',
                    args=dict(
                        name='firewalld',
                        state='absent',
                        extra_args_precommand='--non-interactive '
                                              '--gpg-auto-import-keys',
                    )
                )
            )
        )

        tasks.append(
            dict(
                name="Enabling docker",
                action=dict(
                    module='shell',
                    args=dict(
                        cmd="systemctl enable --now docker",
                    )
                )
            )
        )

        # TODO(jhesketh): These commands are lifted from dev-rook-ceph. However
        # it appears that the sysctl settings are reset after reboot so they
        # may not be useful here.
        tasks.append(
            dict(
                name="Raising max open files",
                action=dict(
                    module='shell',
                    args=dict(
                        cmd="sysctl -w fs.file-max=1200000",
                    )
                )
            )
        )

        tasks.append(
            dict(
                name="Minimize swappiness",
                action=dict(
                    module='shell',
                    args=dict(
                        cmd="sysctl -w vm.swappiness=0",
                    )
                )
            )
        )

        # TODO(jhesketh): Figure out if this is appropriate for all OpenStack
        #                 clouds.
        config = "\nIPADDR_0={{ ansible_host }}/32"
        config += "\nLABEL_0=Floating\n"
        tasks.append(
            dict(
                name="Add floating IP to eth0",
                action=dict(
                    module='shell',
                    args=dict(
                        cmd='printf "%s" >> /etc/sysconfig/network/ifcfg-eth0'
                            % config,
                    )
                )
            )
        )

        # Alternate approach that likely doesn't require setting --node-ip with
        # kubelet (as it'll default to the floating ip).
        # Set static IP to be the floating,
        # add second IP for the internal network,
        # Create default route,
        # Set up DNS again

        tasks.append(
            dict(
                name="Reboot nodes",
                action=dict(
                    module='reboot',
                )
            )
        )

        tasks.append(
            dict(
                name="Setting iptables on nodes to be permissive",
                action=dict(
                    module='shell',
                    args=dict(
                        cmd="iptables -I INPUT -j ACCEPT && "
                            "iptables -P INPUT ACCEPT",
                    )
                )
            )
        )

        play_source = dict(
            name="Prepare nodes",
            hosts="all",
            tasks=tasks,
            gather_facts="no",
            strategy="mitogen_free",
        )
        return play_source
