# This file is part of pycloudlib. See LICENSE file for license information.
"""LXD Cloud type."""
import io
import re
import textwrap
from abc import abstractmethod
import warnings
import paramiko

from pycloudlib.cloud import BaseCloud
from pycloudlib.lxd.instance import LXDInstance
from pycloudlib.util import subp, UBUNTU_RELEASE_VERSION_MAP
from pycloudlib.constants import LOCAL_UBUNTU_ARCH
from pycloudlib.lxd.defaults import base_vm_profiles


class UnsupportedReleaseException(Exception):
    """Unsupported release exception."""

    msg_tmpl = "Release {} is not supported for LXD{}"

    def __init__(self, release, is_vm):
        """Prepare unsupported release message."""
        vm_msg = ""

        if is_vm:
            vm_msg = " vms"

        super().__init__(
            self.msg_tmpl.format(release, vm_msg)
        )


class _BaseLXD(BaseCloud):
    """LXD Base Cloud Class."""

    _type = 'lxd'
    _daily_remote = 'ubuntu-daily'
    _releases_remote = 'ubuntu'

    def __init__(self, tag, timestamp_suffix=True):
        """Initialize LXD cloud class.

        Args:
            tag: string used to name and tag resources with
            timestamp_suffic: Append a timestamped suffix to the tag string.
        """
        super().__init__(tag, timestamp_suffix)

        # User must manually specify the key pair to be used
        self.key_pair = None

    def clone(self, base, new_instance_name):
        """Create copy of an existing instance or snapshot.

        Uses the `lxc copy` command to create a copy of an existing
        instance or a snapshot. To clone a snapshot then the base
        is `instance_name/snapshot_name` otherwise if base is only
        an existing instance it will clone an instance.

        Args:
            base: base instance or instance/snapshot
            new_instance_name: name of new instance

        Returns:
            The created LXD instance object

        """
        self._log.debug('cloning %s to %s', base, new_instance_name)
        subp(['lxc', 'copy', base, new_instance_name])
        return LXDInstance(new_instance_name)

    def create_profile(
        self, profile_name, profile_config, force=False
    ):
        """Create a lxd profile.

        Create a lxd profile and populate it with the given
        profile config. If the profile already exists, we will
        not recreate it, unless the force parameter is set to True.

        Args:
            profile_name: Name of the profile to be created
            profile_config: Config to be added to the new profile
            force: Force the profile creation if it already exists
        """
        profile_list = subp(["lxc", "profile", "list"])

        if profile_name in profile_list and not force:
            msg = "The profile named {} already exists".format(profile_name)
            self._log.debug(msg)
            print(msg)
            return

        if force:
            self._log.debug(
                "Deleting current profile %s ...", profile_name)
            subp(["lxc", "profile", "delete", profile_name])

        self._log.debug("Creating profile %s ...", profile_name)
        subp(["lxc", "profile", "create", profile_name])
        subp(["lxc", "profile", "edit", profile_name], data=profile_config)

    def delete_instance(self, instance_name, wait=True):
        """Delete an instance.

        Args:
            instance_name: instance name to delete
            wait: wait for delete to complete
        """
        self._log.debug('deleting %s', instance_name)
        inst = self.get_instance(instance_name)
        inst.delete(wait)

    def get_instance(self, instance_id):
        """Get an existing instance.

        Args:
            instance_id: instance name to get

        Returns:
            The existing instance as a LXD instance object

        """
        instance = LXDInstance(instance_id)

        if self.key_pair:
            local_path = "/tmp/{}-authorized-keys".format(instance_id)

            instance.pull_file(
                remote_path="/home/ubuntu/.ssh/authorized_keys",
                local_path=local_path
            )

            with open(local_path, "r") as f:
                if self.key_pair.public_key_content in f.read():
                    instance.key_pair = self.key_pair

        return instance

    def create_key_pair(self):
        """Create and set a ssh key pair to be used by the lxd instance.

        Args:
            name: The name of the pycloudlib instance

        Returns:
            A tuple containing the public and private key created
        """
        key = paramiko.RSAKey.generate(4096)
        priv_str = io.StringIO()

        pub_key = "{} {}".format(key.get_name(), key.get_base64())
        key.write_private_key(priv_str, password=None)

        return pub_key, priv_str.getvalue()

    # pylint: disable=R0914,R0912,R0915
    def _prepare_command(
            self, name, release, ephemeral=False, network=None, storage=None,
            inst_type=None, profile_list=None, user_data=None,
            config_dict=None):
        """Build a the command to be used to launch the LXD instance.

        Args:
            name: string, what to call the instance
            release: string, [<remote>:]<release>, what release to launch
                     (default remote: )
            ephemeral: boolean, ephemeral, otherwise persistent
            network: string, optional, network name to use
            storage: string, optional, storage name to use
            inst_type: string, optional, type to use
            profile_list: list, optional, profile(s) to use
            user_data: used by cloud-init to run custom scripts/configuration
            config_dict: dict, optional, configuration values to pass

        Returns:
            A list of string representing the command to be run to
            launch the LXD instance.
        """
        profile_list = profile_list if profile_list else []
        config_dict = config_dict if config_dict else {}

        if ':' not in release:
            release = self._daily_remote + ':' + release

        self._log.debug("Full release to launch: '%s'", release)
        cmd = ['lxc', 'init', release]

        if name:
            cmd.append(name)

        if self.key_pair:
            ssh_user_data = textwrap.dedent(
                """\
                ssh_authorized_keys:
                    - {}
                """.format(self.key_pair.public_key_content)
            )

            if user_data:
                user_data += "\n{}".format(ssh_user_data)

            if "user.user-data" in config_dict:
                config_dict["user.user-data"] += "\n{}".format(ssh_user_data)

            if not user_data and "user.user-data" not in config_dict:
                user_data = "#cloud-config\n{}".format(ssh_user_data)

        if ephemeral:
            cmd.append('--ephemeral')

        if network:
            cmd.append('--network')
            cmd.append(network)

        if storage:
            cmd.append('--storage')
            cmd.append(storage)

        if inst_type:
            cmd.append('--type')
            cmd.append(inst_type)

        for profile in profile_list:
            cmd.append('--profile')
            cmd.append(profile)

        for key, value in config_dict.items():
            cmd.append('--config')
            cmd.append('%s=%s' % (key, value))

        if user_data:
            if 'user.user-data' in config_dict:
                raise ValueError(
                    "User data cannot be defined in config_dict and also"
                    "passed through user_data. Pick one"
                )
            cmd.append('--config')
            cmd.append('user.user-data=%s' % user_data)

        return cmd

    def init(
            self, name, release, ephemeral=False, network=None, storage=None,
            inst_type=None, profile_list=None, user_data=None,
            config_dict=None):
        """Init a container.

        This will initialize a container, but not launch or start it.
        If no remote is specified pycloudlib default to daily images.

        Args:
            name: string, what to call the instance
            release: string, [<remote>:]<release>, what release to launch
                     (default remote: )
            ephemeral: boolean, ephemeral, otherwise persistent
            network: string, optional, network name to use
            storage: string, optional, storage name to use
            inst_type: string, optional, type to use
            profile_list: list, optional, profile(s) to use
            user_data: used by cloud-init to run custom scripts/configuration
            config_dict: dict, optional, configuration values to pass

        Returns:
            The created LXD instance object

        """
        cmd = self._prepare_command(
            name=name,
            release=release,
            ephemeral=ephemeral,
            network=network,
            storage=storage,
            inst_type=inst_type,
            profile_list=profile_list,
            user_data=user_data,
            config_dict=config_dict
        )

        print(cmd)
        result = subp(cmd)

        if not name:
            name = result.split('Instance name is: ')[1]

        self._log.debug('Created %s', name)

        return LXDInstance(name, self.key_pair)

    def launch(self, image_id, instance_type=None, user_data=None, wait=True,
               name=None, ephemeral=False, network=None, storage=None,
               profile_list=None, config_dict=None, **kwargs):
        """Set up and launch a container.

        This will init and start a container with the provided settings.
        If no remote is specified pycloudlib defaults to daily images.

        Args:
            image_id: string, [<remote>:]<image>, what release to launch
            instance_type: string, type to use
            user_data: used by cloud-init to run custom scripts/configuration
            wait: boolean, wait for instance to start
            name: string, what to call the instance
            ephemeral: boolean, ephemeral, otherwise persistent
            network: string, network name to use
            storage: string, storage name to use
            profile_list: list, profile(s) to use
            config_dict: dict, configuration values to pass

        Returns:
            The created LXD instance object

        """
        instance = self.init(
            name=name,
            release=image_id,
            ephemeral=ephemeral,
            network=network,
            storage=storage,
            inst_type=instance_type,
            profile_list=profile_list,
            user_data=user_data,
            config_dict=config_dict
        )
        instance.start(wait)

        return instance

    def released_image(self, release, arch=LOCAL_UBUNTU_ARCH):
        """Find the LXD fingerprint of the latest released image.

        Args:
            release: string, Ubuntu release to look for
            arch: string, architecture to use

        Returns:
            string, LXD fingerprint of latest image

        """
        self._log.debug('finding released Ubuntu image for %s', release)
        return self._search_for_image(
            remote=self._releases_remote,
            daily=False,
            release=release,
            arch=arch
        )

    def daily_image(self, release, arch=LOCAL_UBUNTU_ARCH):
        """Find the LXD fingerprint of the latest daily image.

        Args:
            release: string, Ubuntu release to look for
            arch: string, architecture to use

        Returns:
            string, LXD fingerprint of latest image

        """
        self._log.debug('finding daily Ubuntu image for %s', release)
        return self._search_for_image(
            remote=self._daily_remote,
            daily=True,
            release=release,
            arch=arch
        )

    @abstractmethod
    def _get_image_hash_key(self, release=None):
        """Get the correct hash key to be used to launch LXD instance.

        When query simplestreams for image information, we receive a
        dictionary of metadata. In that metadata we have the necessary
        information to allows us to launch the required image. However,
        we must know which key to use in the metadata dict to allows
        to launch the image.

        Args:
            release: string, optional, Ubuntu release

        Returns
            A string specifying which key of the metadata dictionary
            should be used to launch the image.
        """
        raise NotImplementedError

    def _search_for_image(
        self, remote, daily, release, arch=LOCAL_UBUNTU_ARCH
    ):
        """Find the LXD fingerprint in a given remote.

        Args:
            remote: string, remote to prepend to image_id
            daily: boolean, search on daily remote
            release: string, Ubuntu release to look for
            arch: string, architecture to use

        Returns:
            string, LXD fingerprint of latest image

        """
        image_data = self._find_image(release, arch, daily=daily)
        image_hash_key = self._get_image_hash_key(release)

        return '%s:%s' % (remote, image_data[image_hash_key])

    def _image_info(self, image_id, image_hash_key=None):
        """Find the image serial of a given LXD image.

        Args:
            image_id: string, LXD image fingerprint
            image_hash_key: string, the metadata key used to launch the image

        Returns:
            dict, image info available for the image_id

        """
        daily = True
        if ':' in image_id:
            remote = image_id[:image_id.index(':')]
            image_id = image_id[image_id.index(':')+1:]
            if remote == self._releases_remote:
                daily = False
            elif remote != self._daily_remote:
                raise RuntimeError('Unknown remote: %s' % remote)

        if not image_hash_key:
            image_hash_key = self._get_image_hash_key()

        filters = ['%s=%s' % (image_hash_key, image_id)]
        image_info = self._streams_query(filters, daily=daily)

        return image_info

    def image_serial(self, image_id):
        """Find the image serial of a given LXD image.

        Args:
            image_id: string, LXD image fingerprint

        Returns:
            string, serial of latest image

        """
        self._log.debug(
            'finding image serial for LXD Ubuntu image %s', image_id)

        image_info = self._image_info(image_id)

        return image_info[0]['version_name']

    def delete_image(self, image_id):
        """Delete the image.

        Args:
            image_id: string, LXD image fingerprint
        """
        self._log.debug("Deleting image: '%s'", image_id)

        subp(['lxc', 'image', 'delete', image_id])
        self._log.debug('Deleted %s', image_id)

    def snapshot(self, instance, clean=True, name=None):
        """Take a snapshot of the passed in instance for use as image.

        :param instance: The instance to create an image from
        :type instance: LXDInstance
        :param clean: Whether to call cloud-init clean before creation
        :param wait: Whether to wait until before image is created
            before returning
        :param name: Name of the new image
        :param stateful: Whether to use an LXD stateful snapshot
        """
        if clean:
            instance.clean()

        return instance.snapshot(name)

    def _find_image(self, release, arch=LOCAL_UBUNTU_ARCH, daily=True):
        """Find the latest image for a given release.

        Args:
            release: string, Ubuntu release to look for
            arch: string, architecture to use

        Returns:
            list of dictionaries of images

        """
        filters = [
            'datatype=image-downloads',
            'ftype=lxd.tar.xz',
            'arch=%s' % arch,
            'release=%s' % release,
        ]

        return self._streams_query(filters, daily)[0]


class LXDContainer(_BaseLXD):
    """LXD Containers Cloud Class."""

    TRUSTY_CONTAINER_HASH_KEY = "combined_rootxz_sha256"
    CONTAINER_HASH_KEY = "combined_squashfs_sha256"

    def _get_image_hash_key(self, release=None):
        """Get the correct hash key to be used to launch LXD instance.

        When query simplestreams for image information, we receive a
        dictionary of metadata. In that metadata we have the necessary
        information to allows us to launch the required image. However,
        we must know which key to use in the metadata dict to allows
        to launch the image.

        Args:
            release: string, optional, Ubuntu release

        Returns
            A string specifying which key of the metadata dictionary
            should be used to launch the image.
        """
        if release == "trusty":
            return self.TRUSTY_CONTAINER_HASH_KEY

        return self.CONTAINER_HASH_KEY

    def _image_info(self, image_id, image_hash_key=None):
        """Find the image serial of a given LXD image.

        Args:
            image_id: string, LXD image fingerprint
            image_hash_key: string, the metadata key used to launch the image

        Returns:
            dict, image info available for the image_id

        """
        image_info = super()._image_info(
            image_id=image_id,
            image_hash_key=self.CONTAINER_HASH_KEY
        )

        if not image_info:
            # If this is a trusty image, the hash key for it is different.
            # We will perform a second query for this situation.
            image_info = super()._image_info(
                image_id=image_id,
                image_hash_key=self.TRUSTY_CONTAINER_HASH_KEY
            )

        return image_info


class LXD(LXDContainer):
    """Old LXD Container Cloud Class (Kept for compatibility issues)."""

    def __init__(self, *args, **kwargs):
        """Run LXDContainer constructor."""
        warnings.warn("LXD class is deprecated; use LXDContainer instead.")
        super().__init__(*args, **kwargs)


class LXDVirtualMachine(_BaseLXD):
    """LXD Virtual Machine Cloud Class."""

    XENIAL_IMAGE_VSOCK_SUPPORT = "images:ubuntu/16.04/cloud"
    VM_HASH_KEY = "combined_disk1-img_sha256"

    def _extract_release_from_image_id(self, image_id):
        """Extract the base release from the image_id.

        Args:
            image_id: string, [<remote>:]<release>, what release to launch
                     (default remote: )

        Returns:
            A string containing the base release from the image_id that is used
            to launch the image.
        """
        release_regex = (
            "(.*ubuntu.*(?P<release>(" +
            "|".join(UBUNTU_RELEASE_VERSION_MAP) + "|" +
            "|".join(UBUNTU_RELEASE_VERSION_MAP.values()) +
            ")).*)"
        )
        ubuntu_match = re.match(release_regex, image_id)
        if ubuntu_match:
            release = ubuntu_match.groupdict()["release"]
            for codename, version in UBUNTU_RELEASE_VERSION_MAP.items():
                if release in (codename, version):
                    return codename

        # If we have a hash in the image_id we need to query simplestreams to
        # identify the release.
        return self._image_info(image_id)[0]["release"]

    def build_necessary_profiles(self, release=None):
        """Build necessary profiles to launch the LXD instance.

        Args:
            release: string, [<remote>:]<release>, what release to launch
                     (default remote: )

        Returns:
            A list containing the profiles created
        """
        base_release = self._extract_release_from_image_id(release)
        profile_name = "pycloudlib-vm-{}".format(base_release)

        self.create_profile(
            profile_name=profile_name,
            profile_config=base_vm_profiles[base_release]
        )

        return [profile_name]

    def _prepare_command(
            self, name, release, ephemeral=False, network=None, storage=None,
            inst_type=None, profile_list=None, user_data=None,
            config_dict=None):
        """Build a the command to be used to launch the LXD instance.

        Args:
            name: string, what to call the instance
            release: string, [<remote>:]<release>, what release to launch
                     (default remote: )
            ephemeral: boolean, ephemeral, otherwise persistent
            network: string, optional, network name to use
            storage: string, optional, storage name to use
            inst_type: string, optional, type to use
            profile_list: list, optional, profile(s) to use
            user_data: used by cloud-init to run custom scripts/configuration
            config_dict: dict, optional, configuration values to pass

        Returns:
            A list of string representing the command to be run to
            launch the LXD instance.
        """
        if not profile_list:
            profile_list = self.build_necessary_profiles(release=release)

        cmd = super()._prepare_command(
            name=name,
            release=release,
            ephemeral=ephemeral,
            network=network,
            storage=storage,
            inst_type=inst_type,
            profile_list=profile_list,
            user_data=user_data,
            config_dict=config_dict
        )

        cmd.append("--vm")

        return cmd

    def _get_image_hash_key(self, release=None):
        """Get the correct hash key to be used to launch LXD instance.

        When query simplestreams for image information, we receive a
        dictionary of metadata. In that metadata we have the necessary
        information to allows us to launch the required image. However,
        we must know which key to use in the metadata dict to allows
        to launch the image.

        Args:
            release: string, optional, Ubuntu release

        Returns
            A string specifying which key of the metadata dictionary
            should be used to launch the image.
        """
        return self.VM_HASH_KEY

    def _search_for_image(
        self, remote, daily, release, arch=LOCAL_UBUNTU_ARCH
    ):
        """Find the LXD fingerprint in a given remote.

        Args:
            remote: string, remote to prepend to image_id
            daily: boolean, search on daily remote
            release: string, Ubuntu release to look for
            arch: string, architecture to use

        Returns:
            string, LXD fingerprint of latest image

        """
        if release == "xenial":
            # xenial needs to launch images:ubuntu/16.04/cloud
            # because it contains the HWE kernel which has vhost-vsock support
            self._log.debug(
                "Xenial needs to use %s image because of lxd-agent support",
                self.XENIAL_IMAGE_VSOCK_SUPPORT
            )
            return self.XENIAL_IMAGE_VSOCK_SUPPORT

        if release == "trusty":
            # trusty is not supported on LXD vms
            raise UnsupportedReleaseException(
                release="trusty",
                is_vm=True
            )

        return super()._search_for_image(
            remote=remote,
            daily=daily,
            release=release,
            arch=arch
        )

    def image_serial(self, image_id):
        """Find the image serial of a given LXD image.

        Args:
            image_id: string, LXD image fingerprint

        Returns:
            string, serial of latest image

        """
        if image_id == self.XENIAL_IMAGE_VSOCK_SUPPORT:
            return None

        return super().image_serial(image_id=image_id)
